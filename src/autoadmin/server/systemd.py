"""Linux backend: the launcher runs as a `systemd --user` service."""

from __future__ import annotations

import asyncio
import getpass
import shlex
import time
from pathlib import Path

from . import console
from .backend import LaunchSpec, ProcessState, launcher_argv

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


async def run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


def render_unit(spec: LaunchSpec, argv: list[str], stop_timeout_s: int) -> str:
    # KillMode=mixed: SIGTERM goes to the launcher only, which sends `stop` to the
    # server; everything left is SIGKILLed after TimeoutStopSec.
    return f"""\
# Managed by AutoAdmin. Changes here are overwritten on every start.
[Unit]
Description=AutoAdmin Minecraft server

[Service]
Type=simple
WorkingDirectory={spec.workdir}
ExecStart={shlex.join(argv)}
StandardInput=null
KillMode=mixed
Nice={spec.nice}
MemoryMax={spec.memory_max_mb}M
TimeoutStopSec={stop_timeout_s}
SuccessExitStatus=0 143
Restart=no
"""


class Unit:
    def __init__(self, name: str):
        self.name = name
        self.service = f"{name}.service"
        self.path = UNIT_DIR / self.service

    async def install(self, text: str) -> None:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.read_text() != text:
            self.path.write_text(text)
        code, out = await run("systemctl", "--user", "daemon-reload")
        if code:
            raise RuntimeError(f"systemctl daemon-reload failed: {out}")

    async def start(self) -> None:
        await run("systemctl", "--user", "reset-failed", self.service)
        code, out = await run("systemctl", "--user", "start", self.service)
        if code:
            raise RuntimeError(f"systemctl start failed: {out}")

    async def stop(self) -> None:
        await run("systemctl", "--user", "stop", self.service)

    async def state(self) -> ProcessState:
        _, out = await run("systemctl", "--user", "show", self.service,
                           "-p", "ActiveState", "-p", "ExecMainStatus", "-p", "Result")
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        status = props.get("ExecMainStatus", "")
        return ProcessState(props.get("ActiveState", "inactive"),
                            int(status) if status.isdigit() else None,
                            props.get("Result", ""))

    async def accounting(self) -> dict:
        """Live resource counters from systemd's cgroup accounting."""
        _, out = await run("systemctl", "--user", "show", self.service,
                           "-p", "ActiveState", "-p", "MemoryCurrent", "-p", "MemoryPeak",
                           "-p", "MemoryMax", "-p", "CPUUsageNSec",
                           "-p", "ActiveEnterTimestampMonotonic")
        p = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

        def num(key: str) -> int:
            v = p.get(key, "")
            return int(v) if v.isdigit() else 0

        entered = num("ActiveEnterTimestampMonotonic") / 1e6
        return {
            "running": p.get("ActiveState") in ("active", "activating", "deactivating"),
            "memory": num("MemoryCurrent"),
            "memory_peak": num("MemoryPeak"),
            "memory_max": num("MemoryMax"),
            "cpu_ns": num("CPUUsageNSec"),
            "uptime_s": time.monotonic() - entered if entered else 0,
        }


class SystemdBackend:
    def __init__(self, unit_name: str, run_dir: Path, stop_timeout_s: int):
        self.unit = Unit(unit_name)
        self.socket = console.socket_path(unit_name, run_dir)
        self.exit_file = run_dir / f"{unit_name}.exit"
        self.stop_timeout_s = stop_timeout_s

    async def start(self, spec: LaunchSpec) -> None:
        self.exit_file.parent.mkdir(parents=True, exist_ok=True)
        argv = launcher_argv(self.socket, spec.console_log, self.exit_file, spec)
        await self.unit.install(render_unit(spec, argv, self.stop_timeout_s))
        await self.unit.start()

    async def stop(self) -> None:
        await self.unit.stop()       # blocks until stopped or force-killed

    async def state(self) -> ProcessState:
        return await self.unit.state()

    async def accounting(self) -> dict:
        return await self.unit.accounting()

    async def command(self, command: str, *, until: str | None = None,
                      timeout: float = console.DEFAULT_TIMEOUT) -> list[str]:
        return await console.send(self.socket, command, until=until, timeout=timeout)

    async def survives_logout(self) -> bool:
        return await linger_enabled()


async def linger_enabled() -> bool:
    _, out = await run("loginctl", "show-user", getpass.getuser(), "-p", "Linger")
    return out.strip() == "Linger=yes"
