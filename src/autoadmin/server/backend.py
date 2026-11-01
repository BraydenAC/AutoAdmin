"""How the Minecraft server process is run, per platform.

The manager only talks to a ProcessBackend. Every backend runs the server under
the launcher (`server/launcher.py`), which owns the console, so console commands
work the same everywhere.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .console import DEFAULT_TIMEOUT


@dataclass
class ProcessState:
    active: str            # active | activating | inactive | failed | deactivating
    exit_status: int | None
    result: str            # success | exit-code | signal | oom-kill | timeout | ...

    @property
    def running(self) -> bool:
        return self.active in ("active", "activating", "deactivating")


@dataclass
class LaunchSpec:
    workdir: Path
    java: Path
    heap_mb: int
    launch: list[str]      # args after the JVM memory flags, e.g. ["-jar", "server.jar"]
    memory_max_mb: int
    nice: int
    console_log: Path

    def java_argv(self) -> list[str]:
        return [str(self.java), f"-Xmx{self.heap_mb}M", f"-Xms{min(self.heap_mb, 1024)}M",
                *self.launch, "nogui"]


class ProcessBackend(Protocol):
    async def start(self, spec: LaunchSpec) -> None:
        """Launch and return once the process is running (not once the server is ready)."""

    async def stop(self) -> None:
        """Ask the server to stop and wait; force it after the stop timeout."""

    async def state(self) -> ProcessState: ...

    async def accounting(self) -> dict:
        """running, memory, memory_peak, memory_max (bytes), cpu_ns, uptime_s."""

    async def command(self, command: str, *, until: str | None = None,
                      timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        """Run one console command; raises console.ConsoleError if it can't."""

    async def survives_logout(self) -> bool: ...


def launcher_argv(socket: Path, console_log: Path, exit_file: Path, spec: LaunchSpec) -> list[str]:
    # The bot's own interpreter, so the launcher comes from the same install.
    return [sys.executable, "-m", "autoadmin.server.launcher", "--socket", str(socket),
            "--console-log", str(console_log), "--exit-file", str(exit_file),
            "--", *spec.java_argv()]


def make_backend(unit_name: str, run_dir: Path, stop_timeout_s: int) -> ProcessBackend:
    if sys.platform == "linux":
        from .systemd import SystemdBackend
        return SystemdBackend(unit_name, run_dir, stop_timeout_s)
    return UnsupportedBackend()


class UnsupportedBackend:
    """Placeholder until the Windows backend exists: reports stopped, refuses to start."""

    async def start(self, spec: LaunchSpec) -> None:
        raise RuntimeError(f"running the server on {sys.platform} isn't supported yet")

    async def stop(self) -> None:
        pass

    async def state(self) -> ProcessState:
        return ProcessState("inactive", None, "")

    async def accounting(self) -> dict:
        return {"running": False}

    async def command(self, command: str, *, until: str | None = None,
                      timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        from .console import ConsoleError
        raise ConsoleError("the server isn't running")

    async def survives_logout(self) -> bool:
        return False
