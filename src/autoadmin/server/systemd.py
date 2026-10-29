"""The Minecraft server as a `systemd --user` service."""

from __future__ import annotations

import asyncio
import getpass
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

UNIT_DIR = Path.home() / ".config" / "systemd" / "user"


async def run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace").strip()


@dataclass
class UnitSpec:
    name: str
    workdir: Path
    java: Path
    heap_mb: int
    launch: list[str]          # args after the JVM memory flags, e.g. ["-jar", "server.jar"]
    memory_max_mb: int
    nice: int
    console_log: Path

    def render(self) -> str:
        exec_start = shlex.join([str(self.java), f"-Xmx{self.heap_mb}M", f"-Xms{min(self.heap_mb, 1024)}M",
                                 *self.launch, "nogui"])
        return f"""\
# Managed by AutoAdmin. Changes here are overwritten on every start.
[Unit]
Description=AutoAdmin Minecraft server

[Service]
Type=simple
WorkingDirectory={self.workdir}
ExecStart={exec_start}
StandardInput=null
StandardOutput=truncate:{self.console_log}
StandardError=inherit
Nice={self.nice}
MemoryMax={self.memory_max_mb}M
TimeoutStopSec=90
SuccessExitStatus=0 143
Restart=no
"""


@dataclass
class UnitState:
    active: str       # active | activating | inactive | failed | deactivating
    exit_status: int | None
    result: str

    @property
    def running(self) -> bool:
        return self.active in ("active", "activating", "deactivating")


class Unit:
    def __init__(self, name: str):
        self.name = name
        self.service = f"{name}.service"
        self.path = UNIT_DIR / self.service

    async def install(self, spec: UnitSpec) -> None:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        text = spec.render()
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

    async def state(self) -> UnitState:
        _, out = await run("systemctl", "--user", "show", self.service,
                           "-p", "ActiveState", "-p", "ExecMainStatus", "-p", "Result")
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        status = props.get("ExecMainStatus", "")
        return UnitState(props.get("ActiveState", "inactive"),
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


async def linger_enabled() -> bool:
    _, out = await run("loginctl", "show-user", getpass.getuser(), "-p", "Linger")
    return out.strip() == "Linger=yes"
