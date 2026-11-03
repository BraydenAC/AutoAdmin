"""Windows backend: the launcher runs as a background process the backend owns.

There is no systemd to supervise it, so this backend does that part itself:
- `start` spawns the launcher with no console window, below-normal priority,
  and outside the bot's Task Scheduler job where Windows allows it, so the
  server keeps running when the bot restarts.
- The launcher holds a lock file while it runs and answers on its endpoint;
  when it exits it leaves the exit file, which says how the server ended.
- `stop` asks the launcher to stop the server, waits, then kills the launcher;
  its job object takes Java down with it.

Only the process-creation flags need Windows, so the tests run this on Linux.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import console, lockfile
from .backend import LaunchSpec, ProcessState, launcher_argv

START_TIMEOUT_S = 30
KILL_WAIT_S = 10


def console_python() -> Path:
    """The bot may run under pythonw; the launcher needs python.exe so its output
    handles work (it still gets no window)."""
    exe = Path(sys.executable)
    python = exe.with_name("python.exe")
    return python if exe.name.lower() == "pythonw.exe" and python.exists() else exe


class WindowsBackend:
    def __init__(self, unit_name: str, run_dir: Path, stop_timeout_s: int):
        self.endpoint = run_dir / f"{unit_name}.endpoint.json"
        self.lock_file = run_dir / f"{unit_name}.lock"
        self.exit_file = run_dir / f"{unit_name}.exit"
        self.log_file = run_dir / f"{unit_name}.launcher.log"
        self.stop_timeout_s = stop_timeout_s
        self._proc: subprocess.Popen | None = None

    # -- lifecycle -----------------------------------------------------------------

    async def start(self, spec: LaunchSpec) -> None:
        if lockfile.held(self.lock_file):
            raise RuntimeError("the server's launcher is already running")
        self.exit_file.parent.mkdir(parents=True, exist_ok=True)
        self.endpoint.unlink(missing_ok=True)
        self.exit_file.unlink(missing_ok=True)
        argv = launcher_argv(self.endpoint, spec.console_log, self.exit_file, spec,
                             python=console_python(),
                             extra=["--memory-max-mb", str(spec.memory_max_mb),
                                    "--lock-file", str(self.lock_file)])
        self._proc = await asyncio.to_thread(self._spawn, argv, spec)
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.endpoint.exists() or self._proc.poll() is not None:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError(f"the server's launcher didn't start; see {self.log_file}")

    def _spawn(self, argv: list[str], spec: LaunchSpec) -> subprocess.Popen:
        with self.log_file.open("wb") as log:
            kw = dict(cwd=spec.workdir, stdin=subprocess.DEVNULL, stdout=log,
                      stderr=subprocess.STDOUT)
            if sys.platform != "win32":
                return subprocess.Popen(argv, start_new_session=True, **kw)
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if spec.nice > 0:
                flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
            try:
                return subprocess.Popen(argv, creationflags=flags
                                        | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError as e:
                if getattr(e, "winerror", None) != 5:
                    raise
                # The bot's job doesn't allow breaking away: the server then stops
                # whenever the bot's task is ended.
                return subprocess.Popen(argv, creationflags=flags, **kw)

    async def stop(self) -> None:
        if not lockfile.held(self.lock_file):
            return
        pid = self._pid()
        try:
            await console.control(self.endpoint, "stop")
        except console.ConsoleError:
            pass
        if await self._wait_exit(self.stop_timeout_s):
            return
        # Not stopped in time. The lock is still held, so the pid is still the launcher's.
        if pid is not None and lockfile.held(self.lock_file):
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # TerminateProcess
            except OSError:
                pass
            await self._wait_exit(KILL_WAIT_S)
        self.exit_file.write_text(json.dumps({
            "exit_status": 1, "stop_requested": True, "forced": True, "at": time.time()}))
        self.endpoint.unlink(missing_ok=True)

    async def _wait_exit(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not lockfile.held(self.lock_file):
                return True
            await asyncio.sleep(0.5)
        return False

    def _pid(self) -> int | None:
        try:
            return int(json.loads(self.endpoint.read_text())["pid"])
        except (OSError, ValueError, KeyError):
            return None

    # -- queries -------------------------------------------------------------------

    async def state(self) -> ProcessState:
        # The lock first: pinging a dead launcher's port is slow on Windows.
        if lockfile.held(self.lock_file):
            try:
                info = await console.control(self.endpoint, "ping")
            except console.ConsoleError:
                return ProcessState("activating", None, "")    # starting, or not answering
            return ProcessState("deactivating" if info.get("stopping") else "active", None, "")
        if self.exit_file.exists():
            return self._exited()
        if self._proc is not None:
            if self._proc.poll() is None:
                return ProcessState("activating", None, "")    # not holding the lock yet
            # Died before it could record anything, e.g. a broken install.
            return ProcessState("failed", self._proc.returncode, "exit-code")
        if self.endpoint.exists():
            return ProcessState("failed", None, "signal")      # killed, or the machine went down
        return ProcessState("inactive", None, "")

    def _exited(self) -> ProcessState:
        try:
            info = json.loads(self.exit_file.read_text())
        except (OSError, ValueError):
            return ProcessState("failed", None, "")
        status = info.get("exit_status")
        if info.get("forced"):
            return ProcessState("failed", status, "timeout")
        if status == 0:
            return ProcessState("inactive", 0, "success")
        if info.get("memory_limit_hit"):
            return ProcessState("failed", status, "oom-kill")
        return ProcessState("failed", status, "exit-code")

    async def accounting(self) -> dict:
        if not lockfile.held(self.lock_file):
            return {"running": False}
        try:
            s = await console.control(self.endpoint, "stats")
        except console.ConsoleError:
            return {"running": False}
        return {"running": True, "memory": s.get("memory", 0),
                "memory_peak": s.get("memory_peak", 0), "memory_max": s.get("memory_max", 0),
                "cpu_ns": s.get("cpu_ns", 0), "uptime_s": s.get("uptime_s", 0)}

    async def command(self, command: str, *, until: str | None = None,
                      timeout: float = console.DEFAULT_TIMEOUT) -> list[str]:
        return await console.send(self.endpoint, command, until=until, timeout=timeout)

    async def survives_logout(self) -> bool:
        from ..service import bot_task_always_on
        return await asyncio.to_thread(bot_task_always_on)
