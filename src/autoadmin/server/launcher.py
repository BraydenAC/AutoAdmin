"""Runs the Minecraft server JVM and owns its console.

    python -m autoadmin.server.launcher --endpoint E --console-log L --exit-file X -- java ...

- Java's stdin belongs to this process. Console commands arrive over a private
  local channel (see `console`), so no network port (RCON) is needed.
- Java's output is copied to the console log and to any command waiting for
  its response.
- SIGTERM, or a `stop` control request on Windows, sends `stop` to the server
  so it saves and exits; the backend force-kills after its own timeout.
- On Windows, Java runs in a Job Object (`winjob`) that caps its memory and
  kills it if this process dies.
- When Java exits, its status goes to the exit file and becomes this
  process's exit status, so the service manager sees the server's real result.

Stdlib only, and nothing here imports the rest of AutoAdmin except `console`,
`lockfile`, `winjob` and `fileperm`.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..fileperm import restrict_to_owner
from . import lockfile
from .console import DEFAULT_TIMEOUT, MAX_TIMEOUT, ConsoleError, check_command, is_tcp

# "[12:00:00] [Server thread/INFO]: msg" and "[...] [...] [minecraft/X]: msg" -> "msg"
LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+:\s?")
# After a command's first output line, this much silence ends its response.
QUIET_S = 0.3
LINE_LIMIT = 1 << 20


def strip_prefix(line: str) -> str:
    return LOG_PREFIX.sub("", line, count=1)


class Launcher:
    def __init__(self, argv: list[str], endpoint: Path, console_log: Path, exit_file: Path,
                 memory_max_mb: int | None = None, lock_file: Path | None = None):
        self.argv, self.endpoint, self.console_log, self.exit_file = \
            argv, endpoint, console_log, exit_file
        self.memory_max_mb, self.lock_file = memory_max_mb, lock_file
        self.proc: asyncio.subprocess.Process | None = None
        self.job = None
        self.token = secrets.token_urlsafe(32) if is_tcp(endpoint) else None
        self.started = time.monotonic()
        self._listeners: list[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    async def run(self) -> int:
        # Held until this process exits; tells the backend the launcher is alive.
        lock = lockfile.hold(self.lock_file) if self.lock_file else None
        try:
            return await self._run()
        finally:
            if lock:
                lock.close()

    async def _run(self) -> int:
        self.exit_file.unlink(missing_ok=True)
        # Endpoint, signal handlers and job first, so a failure or an early stop
        # can't leave a JVM running that nothing controls.
        server = await self._serve()
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW
            self.job = self._make_job()
        else:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_stop)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, limit=LINE_LIMIT, creationflags=flags)
        self.started = time.monotonic()
        if self.job:
            try:
                self.job.assign(self.proc.pid)
            except OSError as e:
                print(f"launcher: no memory cap, couldn't add Java to the job: {e}",
                      file=sys.stderr, flush=True)
        if self._stopping:
            self._stopping = False
            self.request_stop()
        pump = asyncio.create_task(self._pump())
        code = await self.proc.wait()
        await pump
        server.close()
        self.endpoint.unlink(missing_ok=True)
        status = code if code >= 0 else 128 - code      # killed by signal N -> 128+N
        self.exit_file.write_text(json.dumps({
            "exit_status": status, "signal": -code if code < 0 else None,
            "stop_requested": self._stopping, "memory_limit_hit": self._hit_limit(),
            "at": time.time()}))
        return status

    def _make_job(self):
        from .winjob import Job
        try:
            return Job(self.memory_max_mb)
        except OSError as e:
            print(f"launcher: no memory cap, couldn't create a job object: {e}",
                  file=sys.stderr, flush=True)
            return None

    def _hit_limit(self) -> bool:
        try:
            return bool(self.job and self.job.hit_memory_limit())
        except OSError:
            return False

    async def _serve(self) -> asyncio.AbstractServer:
        self.endpoint.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint.unlink(missing_ok=True)
        if self.token:
            return await self._serve_tcp()
        old = os.umask(0o077)     # socket readable by this user only
        try:
            return await asyncio.start_unix_server(self._client, str(self.endpoint))
        finally:
            os.umask(old)

    async def _serve_tcp(self) -> asyncio.AbstractServer:
        server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        # Any local user can reach the port, so requests need the token, and
        # the file holding it is made owner-only before the token is written.
        tmp = self.endpoint.with_name(self.endpoint.name + ".tmp")
        tmp.unlink(missing_ok=True)
        os.close(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        restrict_to_owner(tmp)
        tmp.write_text(json.dumps({"port": port, "token": self.token, "pid": os.getpid()}))
        os.replace(tmp, self.endpoint)
        return server

    def request_stop(self) -> None:
        proc = self.proc
        if proc is None:
            self._stopping = True     # not started yet; run() stops it right away
            return
        if proc.returncode is not None:
            return
        if self._stopping:
            proc.terminate()     # asked twice: stop waiting for a clean save
            return
        self._stopping = True
        try:
            proc.stdin.write(b"stop\n")
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            proc.terminate()

    async def _pump(self) -> None:
        with self.console_log.open("wb") as log:
            while True:
                try:
                    raw = await self.proc.stdout.readline()
                except ValueError:       # absurdly long line: keep what fits
                    raw = await self.proc.stdout.read(LINE_LIMIT)
                if not raw:
                    return
                log.write(raw)
                log.flush()
                line = raw.decode(errors="replace").rstrip("\r\n")
                for q in self._listeners:
                    q.put_nowait(line)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            req = json.loads(await asyncio.wait_for(reader.readline(), 10))
            if self.token and not hmac.compare_digest(str(req.get("token", "")), self.token):
                raise ConsoleError("bad token")
            if action := req.get("control"):
                resp = {"ok": True, **self.control(str(action))}
            else:
                timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
                output = await self.command(str(req.get("command", "")), req.get("until"), timeout)
                resp = {"ok": True, "output": output}
        except (ConsoleError, ValueError, TimeoutError, AttributeError) as e:
            resp = {"ok": False, "error": str(e) or type(e).__name__}
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    def control(self, action: str) -> dict:
        if action == "ping":
            return {"pid": os.getpid(), "stopping": self._stopping}
        if action == "stop":
            self.request_stop()
            return {}
        if action == "stats":
            stats = {"uptime_s": time.monotonic() - self.started}
            try:
                return {**stats, **(self.job.stats() if self.job else {})}
            except OSError:
                return stats
        raise ConsoleError(f"unknown control request {action!r}")

    async def command(self, command: str, until: str | None, timeout: float) -> list[str]:
        command = check_command(command)
        pattern = re.compile(until) if until else None
        async with self._lock:     # one at a time, so responses don't interleave
            if self.proc is None or self.proc.returncode is not None or self._stopping:
                raise ConsoleError("the server is stopping")
            q: asyncio.Queue[str] = asyncio.Queue()
            self._listeners.append(q)
            try:
                self.proc.stdin.write(command.encode() + b"\n")
                await self.proc.stdin.drain()
                return await self._collect(q, pattern, timeout)
            except (BrokenPipeError, ConnectionResetError) as e:
                raise ConsoleError("the server's console is closed") from e
            finally:
                self._listeners.remove(q)

    @staticmethod
    async def _collect(q: asyncio.Queue[str], pattern: re.Pattern | None,
                       timeout: float) -> list[str]:
        lines: list[str] = []
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            wait = remaining if pattern or not lines else min(QUIET_S, remaining)
            try:
                line = await asyncio.wait_for(q.get(), wait)
            except TimeoutError:
                if lines and not pattern:
                    break
                continue
            lines.append(strip_prefix(line))
            if pattern and pattern.search(line):
                return lines
        if pattern:
            raise ConsoleError(f"timed out after {timeout:.0f}s waiting for the server")
        return lines


def main() -> None:
    p = argparse.ArgumentParser(prog="autoadmin-launcher")
    p.add_argument("--endpoint", "--socket", type=Path, required=True)
    p.add_argument("--console-log", type=Path, required=True)
    p.add_argument("--exit-file", type=Path, required=True)
    p.add_argument("--memory-max-mb", type=int)     # Windows job cap; systemd has MemoryMax
    p.add_argument("--lock-file", type=Path)        # held while running (Windows backend)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    args = p.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        p.error("no server command given after --")
    sys.exit(asyncio.run(Launcher(argv, args.endpoint, args.console_log, args.exit_file,
                                  args.memory_max_mb, args.lock_file).run()))


if __name__ == "__main__":
    main()
