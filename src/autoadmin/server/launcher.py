"""Runs the Minecraft server JVM and owns its console.

    python -m autoadmin.server.launcher --socket S --console-log L --exit-file E -- java ...

- Java's stdin belongs to this process. Console commands arrive over a private
  local socket, so no network port (RCON) is needed.
- Java's output is copied to the console log and to any command waiting for
  its response.
- SIGTERM sends `stop` to the server so it saves and exits; the service manager
  force-kills after its own timeout.
- When Java exits, its status goes to the exit file and becomes this
  process's exit status, so the service manager sees the server's real result.

Stdlib only, and nothing here imports the rest of AutoAdmin except `console`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

from .console import DEFAULT_TIMEOUT, MAX_TIMEOUT, ConsoleError, check_command

# "[12:00:00] [Server thread/INFO]: msg" and "[...] [...] [minecraft/X]: msg" -> "msg"
LOG_PREFIX = re.compile(r"^(?:\[[^\]]*\]\s*)+:\s?")
# After a command's first output line, this much silence ends its response.
QUIET_S = 0.3
LINE_LIMIT = 1 << 20


def strip_prefix(line: str) -> str:
    return LOG_PREFIX.sub("", line, count=1)


class Launcher:
    def __init__(self, argv: list[str], socket: Path, console_log: Path, exit_file: Path):
        self.argv, self.socket, self.console_log, self.exit_file = argv, socket, console_log, exit_file
        self.proc: asyncio.subprocess.Process | None = None
        self._listeners: list[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()
        self._stopping = False

    async def run(self) -> int:
        self.exit_file.unlink(missing_ok=True)
        # Socket and signal handlers first, so a failure or an early SIGTERM can't
        # leave a JVM running that nothing controls.
        server = await self._serve()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self.request_stop)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, limit=LINE_LIMIT)
        if self._stopping:
            self._stopping = False
            self.request_stop()
        pump = asyncio.create_task(self._pump())
        code = await self.proc.wait()
        await pump
        server.close()
        self.socket.unlink(missing_ok=True)
        status = code if code >= 0 else 128 - code      # killed by signal N -> 128+N
        self.exit_file.write_text(json.dumps({
            "exit_status": status, "signal": -code if code < 0 else None,
            "stop_requested": self._stopping, "at": time.time()}))
        return status

    async def _serve(self) -> asyncio.AbstractServer:
        self.socket.parent.mkdir(parents=True, exist_ok=True)
        self.socket.unlink(missing_ok=True)
        old = os.umask(0o077)     # socket readable by this user only
        try:
            return await asyncio.start_unix_server(self._client, str(self.socket))
        finally:
            os.umask(old)

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
            timeout = min(float(req.get("timeout") or DEFAULT_TIMEOUT), MAX_TIMEOUT)
            output = await self.command(str(req.get("command", "")), req.get("until"), timeout)
            resp = {"ok": True, "output": output}
        except (ConsoleError, ValueError, TimeoutError) as e:
            resp = {"ok": False, "error": str(e) or type(e).__name__}
        try:
            writer.write(json.dumps(resp).encode() + b"\n")
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

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
    p.add_argument("--socket", type=Path, required=True)
    p.add_argument("--console-log", type=Path, required=True)
    p.add_argument("--exit-file", type=Path, required=True)
    p.add_argument("argv", nargs=argparse.REMAINDER)
    args = p.parse_args()
    argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    if not argv:
        p.error("no server command given after --")
    sys.exit(asyncio.run(Launcher(argv, args.socket, args.console_log, args.exit_file).run()))


if __name__ == "__main__":
    main()
