"""Installs, configures, starts, and stops the single Minecraft server.

Long operations block here, in code, until there is a definite outcome. The
agent only gets a one-line success or a short failure excerpt + incident id.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import tarfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from ..config import Config, Jail
from . import properties, versions
from .downloads import Cache
from .incidents import Incident, IncidentStore, excerpt_failure, read_lines
from .rcon import Rcon, RconError
from .systemd import Unit, UnitSpec, linger_enabled

Progress = Callable[[str], Awaitable[None]]

LOADERS = ("vanilla", "fabric", "neoforge")
DONE = re.compile(r"\]: Done \(\d+[.,]\d+s\)!")
EULA_NEEDED = re.compile(r"You need to agree to the EULA")
EULA_URL = "https://aka.ms/MinecraftEULA"
NEOFORGE_OK = "The server installed successfully"


class ServerError(Exception):
    """An expected failure the agent should see (bad version, wrong state, ...)."""


class StartOutcome(StrEnum):
    READY = "ready"
    EULA_REQUIRED = "eula_required"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ALREADY_RUNNING = "already_running"


@dataclass
class InstallState:
    minecraft_version: str
    loader: str
    loader_version: str | None
    java_major: int
    launch: list[str]
    installed_at: float
    installed_by: str
    rcon_password: str
    memory_mb: int
    eula_accepted_by: str | None = None
    eula_accepted_at: float | None = None
    extra: dict = field(default_factory=dict)


async def _noop(_: str) -> None:
    pass


class ServerManager:
    def __init__(self, config: Config, jail: Jail, incidents: IncidentStore):
        self.cfg = config.server
        self.jail = jail
        self.dir = jail.path("server")
        self.cache = Cache(jail.path("cache"))
        self.incidents = incidents
        self.unit = Unit(self.cfg.unit_name)
        self._state_path = self.dir / "autoadmin.json"
        self._lock = asyncio.Lock()

    # -- state -------------------------------------------------------------------

    def state(self) -> InstallState | None:
        if not self._state_path.exists():
            return None
        return InstallState(**json.loads(self._state_path.read_text()))

    def _save(self, st: InstallState) -> None:
        self._state_path.write_text(json.dumps(asdict(st), indent=2))

    def _require_installed(self) -> InstallState:
        st = self.state()
        if st is None:
            raise ServerError("No server is installed yet.")
        return st

    async def running(self) -> bool:
        return (await self.unit.state()).running

    # -- java --------------------------------------------------------------------

    def _java_bin(self, major: int) -> Path | None:
        found = sorted(self.jail.path("java", str(major)).glob("*/bin/java"))
        return found[0] if found else None

    async def ensure_java(self, major: int, progress: Progress = _noop) -> Path:
        if java := self._java_bin(major):
            return java
        rt = await versions.resolve_java(major)
        await progress(f"Downloading Java {major} runtime ({rt.release})…")
        archive = await self.cache.fetch(rt.url, algo="sha256", digest=rt.sha256)
        dest = self.jail.path("java", str(major))
        tmp = dest.with_name(dest.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)

        def extract() -> None:
            with tarfile.open(archive) as tf:
                tf.extractall(tmp, filter="data")
        await asyncio.to_thread(extract)
        shutil.rmtree(dest, ignore_errors=True)
        os.replace(tmp, dest)
        if not (java := self._java_bin(major)):
            raise ServerError(f"Java {major} archive had no bin/java.")
        return java

    # -- install -----------------------------------------------------------------

    async def install(self, mc_version: str, loader: str, requested_by: str,
                      progress: Progress = _noop) -> str:
        if loader not in LOADERS:
            raise ServerError(f"Unknown loader '{loader}'. Use one of: {', '.join(LOADERS)}.")
        async with self._lock:
            if await self.running():
                raise ServerError("Stop the server before installing a different version.")
            try:
                mc = await versions.resolve_minecraft(mc_version)
            except versions.VersionError as e:
                raise ServerError(str(e))

            java = await self.ensure_java(mc.java_major, progress)
            previous = self._set_aside_previous()

            self.dir.mkdir(parents=True, exist_ok=True)
            loader_version: str | None = None
            try:
                if loader == "vanilla":
                    await progress(f"Downloading Minecraft {mc.id} server…")
                    await self.cache.fetch_to(mc.server_url, self.dir / "server.jar",
                                              algo="sha1", digest=mc.server_sha1)
                    launch = ["-jar", "server.jar"]
                elif loader == "fabric":
                    build = await versions.resolve_fabric(mc.id)
                    loader_version = build.loader
                    await progress(f"Downloading Fabric loader {build.loader} for {mc.id}…")
                    await self.cache.fetch_to(build.url, self.dir / "fabric-server-launch.jar")
                    launch = ["-jar", "fabric-server-launch.jar"]
                else:
                    build = await versions.resolve_neoforge(mc.id)
                    loader_version = build.version
                    await progress(f"Installing NeoForge {build.version} (takes about a minute)…")
                    await self._run_neoforge_installer(java, build)
                    launch = ["@user_jvm_args.txt",
                              f"@libraries/net/neoforged/neoforge/{build.version}/unix_args.txt"]
            except versions.VersionError as e:
                raise ServerError(str(e))

            properties.write(self.dir / "server.properties", {
                "enable-rcon": "true",
                "rcon.port": str(self.cfg.rcon_port),
                "rcon.password": (pw := secrets.token_urlsafe(18)),
                "broadcast-rcon-to-ops": "false",
                "server-port": str(self.cfg.server_port),
                "enable-query": "false",
            })
            st = InstallState(
                minecraft_version=mc.id, loader=loader, loader_version=loader_version,
                java_major=mc.java_major, launch=launch, installed_at=time.time(),
                installed_by=requested_by, rcon_password=pw, memory_mb=self.cfg.memory_mb)
            self._save(st)

        desc = f"Minecraft {mc.id}" + (f" with {loader} {loader_version}" if loader_version else " (vanilla)")
        note = f" Previous install moved to {previous}." if previous else ""
        return f"Installed {desc}, Java {mc.java_major}.{note} EULA not yet accepted."

    def _set_aside_previous(self) -> str | None:
        if not self.dir.exists() or not any(self.dir.iterdir()):
            return None
        target = self.jail.path(f"server.old-{time.strftime('%Y%m%d-%H%M%S')}")
        os.replace(self.dir, target)
        return target.name

    async def _run_neoforge_installer(self, java: Path, build: versions.NeoForgeBuild) -> None:
        installer = await self.cache.fetch(build.installer_url, algo="sha1",
                                           digest=build.installer_sha1)
        log_path = self.jail.path("logs", f"install-neoforge-{build.version}-{int(time.time())}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                str(java), "-jar", str(installer), "--installServer", str(self.dir),
                cwd=self.dir, stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                code = await asyncio.wait_for(proc.wait(), self.cfg.install_timeout_s)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                code = None
        lines = read_lines(log_path)
        if code == 0 and any(NEOFORGE_OK in line for line in lines[-20:]):
            return
        why = "timed out" if code is None else f"exited with code {code}"
        inc = self.incidents.create("install_failed", f"NeoForge installer {why}.",
                                    excerpt_failure(lines), [log_path])
        raise InstallFailed(inc)

    # -- configuration -------------------------------------------------------------

    def accept_eula(self, accepted_by: str) -> str:
        st = self._require_installed()
        (self.dir / "eula.txt").write_text(
            f"# Accepted by {accepted_by} via AutoAdmin at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"# {EULA_URL}\neula=true\n")
        st.eula_accepted_by, st.eula_accepted_at = accepted_by, time.time()
        self._save(st)
        return f"EULA accepted by {accepted_by}."

    def get_properties(self) -> dict[str, str]:
        self._require_installed()
        props = properties.read(self.dir / "server.properties")
        return {k: v for k, v in props.items() if k in properties.EDITABLE or k == "server-port"}

    def set_properties(self, changes: dict[str, str]) -> dict[str, str]:
        self._require_installed()
        try:
            clean = properties.validate(changes)
        except ValueError as e:
            raise ServerError(str(e))
        properties.write(self.dir / "server.properties", clean)
        return clean

    def host_memory_mb(self) -> int:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
        return 0

    def set_memory(self, mb: int) -> str:
        st = self._require_installed()
        total = self.host_memory_mb()
        limit = total - self.cfg.min_host_free_mb - self.cfg.memory_overhead_mb
        if mb < 1024:
            raise ServerError("The server needs at least 1024 MB.")
        if mb > limit:
            raise ServerError(f"{mb} MB would leave the host too little memory; "
                              f"the most allowed on this {total} MB machine is {limit} MB.")
        st.memory_mb = mb
        self._save(st)
        return f"Server memory set to {mb} MB; takes effect on next start."

    # -- lifecycle -------------------------------------------------------------------

    def _rcon(self, st: InstallState) -> Rcon:
        return Rcon("127.0.0.1", self.cfg.rcon_port, st.rcon_password)

    async def start(self, progress: Progress = _noop) -> tuple[StartOutcome, str]:
        async with self._lock:
            st = self._require_installed()
            if await self.running():
                return StartOutcome.ALREADY_RUNNING, "The server is already running."
            java = self._java_bin(st.java_major) or await self.ensure_java(st.java_major, progress)
            console = self.dir / "console.log"
            await self.unit.install(UnitSpec(
                name=self.unit.name, workdir=self.dir, java=java, heap_mb=st.memory_mb,
                launch=st.launch, memory_max_mb=st.memory_mb + self.cfg.memory_overhead_mb,
                nice=self.cfg.nice, console_log=console))
            started = time.time()
            await self.unit.start()
            await progress("Starting the server…")
            return await self._await_startup(console, started)

    async def _await_startup(self, console: Path, started: float) -> tuple[StartOutcome, str]:
        deadline = started + self.cfg.start_timeout_s
        while True:
            await asyncio.sleep(1)
            lines = read_lines(console)
            if any(DONE.search(l) for l in lines):
                return StartOutcome.READY, f"Server is up ({time.time() - started:.0f}s)."
            state = await self.unit.state()
            if not state.running:
                if any(EULA_NEEDED.search(l) for l in lines):
                    return (StartOutcome.EULA_REQUIRED,
                            f"The server stopped because the EULA hasn't been accepted ({EULA_URL}).")
                inc = self._incident("start_failed",
                                     f"Server exited during startup (exit status {state.exit_status}).",
                                     lines)
                return StartOutcome.FAILED, inc.brief()
            if time.time() > deadline:
                inc = self._incident("start_timeout",
                                     f"Server still not ready after {self.cfg.start_timeout_s}s; "
                                     "it is still running.", lines)
                return StartOutcome.TIMEOUT, inc.brief()

    def _incident(self, kind: str, summary: str, lines: list[str]) -> Incident:
        crash_dir = self.dir / "crash-reports"
        crash = sorted(crash_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime)[-1:] \
            if crash_dir.exists() else []
        files = [self.dir / "console.log", self.dir / "logs" / "latest.log", *crash]
        return self.incidents.create(kind, summary, excerpt_failure(lines), files)

    async def stop(self) -> str:
        async with self._lock:
            st = self._require_installed()
            if not await self.running():
                return "The server isn't running."
            try:
                await self._rcon(st).command("stop")
            except RconError:
                await self.unit.stop()
            deadline = time.time() + self.cfg.stop_timeout_s
            while await self.running():
                if time.time() > deadline:
                    await self.unit.stop()
                    return "The server didn't stop cleanly in time, so I forced it."
                await asyncio.sleep(1)
            return "Server stopped."

    async def console(self, command: str) -> str:
        st = self._require_installed()
        if not await self.running():
            raise ServerError("The server isn't running.")
        try:
            out = await self._rcon(st).command(command.removeprefix("/"))
        except RconError as e:
            raise ServerError(str(e))
        return out or "(no output)"

    async def status(self) -> dict:
        st = self.state()
        if st is None:
            return {"installed": False}
        unit = await self.unit.state()
        info = {
            "installed": True,
            "minecraft_version": st.minecraft_version,
            "loader": st.loader if not st.loader_version else f"{st.loader} {st.loader_version}",
            "java": st.java_major,
            "memory_mb": st.memory_mb,
            "eula_accepted": st.eula_accepted_by is not None,
            "state": "running" if unit.running else "stopped",
            "survives_logout": await linger_enabled(),
        }
        if unit.running:
            try:
                info["players"] = await self._rcon(st).command("list")
            except RconError:
                info["players"] = "unknown (server still starting?)"
        return info


class InstallFailed(ServerError):
    def __init__(self, incident: Incident):
        super().__init__(incident.brief())
        self.incident = incident
