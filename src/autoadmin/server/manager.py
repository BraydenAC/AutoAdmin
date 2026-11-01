"""Installs, configures, starts, and stops the single Minecraft server.

Long operations block here, in code, until there is a definite outcome. The
agent only gets a one-line success or a short failure excerpt + incident id.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tarfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, fields
from enum import StrEnum
from pathlib import Path

from ..config import Config, Jail
from . import properties, versions
from .downloads import Cache
from .incidents import Incident, IncidentStore, excerpt_failure, read_lines
from .backend import LaunchSpec, ProcessState, make_backend
from .console import ConsoleError

Progress = Callable[[str], Awaitable[None]]

LOADERS = ("vanilla", "fabric", "neoforge")
WORLD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DONE = re.compile(r"\]: Done \(\d+[.,]\d+s\)!")
EULA_NEEDED = re.compile(r"You need to agree to the EULA")
EULA_URL = "https://aka.ms/MinecraftEULA"
NEOFORGE_OK = "The server installed successfully"
# Forced on every install and start: RCON would open a network port.
MANAGED_PROPERTIES = {"enable-rcon": "false", "enable-query": "false"}


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
    memory_mb: int
    eula_accepted_by: str | None = None
    eula_accepted_at: float | None = None
    mods: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)


async def _noop(_: str) -> None:
    pass


class ServerManager:
    def __init__(self, config: Config, jail: Jail, incidents: IncidentStore):
        self.cfg = config.server
        self.jail = jail
        self.worlds_root = jail.path("worlds")
        self.cache = Cache(jail.path("cache"))
        self.incidents = incidents
        self.backend = make_backend(self.cfg.unit_name, jail.path("run"), self.cfg.stop_timeout_s)
        self._active_file = jail.path("active_world")
        self._eula_file = jail.path("eula.json")
        self._desired_file = jail.path("server_desired")
        self._lock = asyncio.Lock()
        self._migrate_single_server()

    def _migrate_single_server(self) -> None:
        """Phase 2 kept one install in runtime/server; it becomes the world 'world'."""
        old = self.jail.path("server")
        if (old / "autoadmin.json").exists() and not self.worlds_root.exists():
            self.worlds_root.mkdir(parents=True)
            os.replace(old, self.worlds_root / "world")
            self._active_file.write_text("world")
            st = self.state()
            if st and st.eula_accepted_by:
                self._eula_file.write_text(json.dumps(
                    {"accepted_by": st.eula_accepted_by, "accepted_at": st.eula_accepted_at}))

    # -- worlds and state ----------------------------------------------------------

    def world_dir(self, name: str) -> Path:
        if not WORLD_NAME.fullmatch(name):
            raise ServerError("World names are 1-32 characters: lowercase letters, digits, - and _.")
        return self.jail.path("worlds", name)

    def active_world(self) -> str | None:
        try:
            name = self._active_file.read_text().strip()
        except FileNotFoundError:
            return None
        return name if name and (self.worlds_root / name / "autoadmin.json").exists() else None

    @property
    def dir(self) -> Path:
        name = self.active_world()
        if name is None:
            raise ServerError("No world is selected. Create one first.")
        return self.worlds_root / name

    def state(self, world: str | None = None) -> InstallState | None:
        name = world or self.active_world()
        if name is None:
            return None
        path = self.worlds_root / name / "autoadmin.json"
        if not path.exists():
            return None
        known = {f.name for f in fields(InstallState)}
        # Older files may carry retired fields (e.g. rcon_password).
        return InstallState(**{k: v for k, v in json.loads(path.read_text()).items() if k in known})

    def save_state(self, st: InstallState, world: str | None = None) -> None:
        name = world or self.active_world()
        (self.worlds_root / name / "autoadmin.json").write_text(json.dumps(asdict(st), indent=2))

    _save = save_state

    def _require_installed(self) -> InstallState:
        st = self.state()
        if st is None:
            raise ServerError("No world is installed yet.")
        return st

    def eula_record(self) -> dict | None:
        try:
            return json.loads(self._eula_file.read_text())
        except FileNotFoundError:
            return None

    def _write_eula_txt(self, target: Path) -> None:
        rec = self.eula_record()
        if rec:
            (target / "eula.txt").write_text(
                f"# Accepted by {rec['accepted_by']} via AutoAdmin\n# {EULA_URL}\neula=true\n")

    def list_worlds(self) -> list[dict]:
        active = self.active_world()
        out = []
        for d in sorted(self.worlds_root.glob("*/autoadmin.json")) if self.worlds_root.exists() else []:
            st = self.state(d.parent.name)
            out.append({
                "name": d.parent.name,
                "active": d.parent.name == active,
                "minecraft_version": st.minecraft_version,
                "loader": st.loader if not st.loader_version else f"{st.loader} {st.loader_version}",
                "mods": len(st.mods),
                "world_generated": (d.parent / properties.read(
                    d.parent / "server.properties").get("level-name", "world")).exists(),
            })
        return out

    async def switch_world(self, name: str) -> str:
        target = self.world_dir(name)
        if not (target / "autoadmin.json").exists():
            raise ServerError(f"There is no world named '{name}'.")
        if await self.running():
            raise ServerError("Stop the server before switching worlds.")
        self._active_file.write_text(name)
        st = self.state(name)
        return f"Switched to '{name}' (Minecraft {st.minecraft_version}, {st.loader})."

    async def delete_world(self, name: str) -> str:
        target = self.world_dir(name)
        if not (target / "autoadmin.json").exists():
            raise ServerError(f"There is no world named '{name}'.")
        if name == self.active_world() and await self.running():
            raise ServerError("Stop the server before deleting the active world.")
        await asyncio.to_thread(shutil.rmtree, target)
        if name == self.active_world() or not self._active_points_somewhere():
            self._active_file.unlink(missing_ok=True)
        return f"Deleted world '{name}'. Its backups were kept."

    def _active_points_somewhere(self) -> bool:
        return self.active_world() is not None

    async def running(self) -> bool:
        return (await self.backend.state()).running

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
                      progress: Progress = _noop, *, world: str,
                      loader_version: str | None = None, memory_mb: int | None = None) -> str:
        """Install a server into a new world folder. `loader_version` pins the loader
        (used when rebuilding a world from a backup)."""
        if loader not in LOADERS:
            raise ServerError(f"Unknown loader '{loader}'. Use one of: {', '.join(LOADERS)}.")
        target = self.world_dir(world)
        if target.exists() and any(target.iterdir()):
            raise ServerError(f"A world named '{world}' already exists.")
        async with self._lock:
            try:
                mc = await versions.resolve_minecraft(mc_version)
            except versions.VersionError as e:
                raise ServerError(str(e))

            java = await self.ensure_java(mc.java_major, progress)
            tmp = target.with_name(target.name + ".installing")
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir(parents=True)
            pinned = loader_version
            try:
                if loader == "vanilla":
                    await progress(f"Downloading Minecraft {mc.id} server…")
                    await self.cache.fetch_to(mc.server_url, tmp / "server.jar",
                                              algo="sha1", digest=mc.server_sha1)
                    launch = ["-jar", "server.jar"]
                elif loader == "fabric":
                    build = await versions.resolve_fabric(mc.id, pinned)
                    loader_version = build.loader
                    await progress(f"Downloading Fabric loader {build.loader} for {mc.id}…")
                    await self.cache.fetch_to(build.url, tmp / "fabric-server-launch.jar")
                    launch = ["-jar", "fabric-server-launch.jar"]
                else:
                    build = await versions.resolve_neoforge(mc.id, pinned)
                    loader_version = build.version
                    await progress(f"Installing NeoForge {build.version} (takes about a minute)…")
                    await self._run_neoforge_installer(java, build, tmp)
                    launch = ["@user_jvm_args.txt",
                              f"@libraries/net/neoforged/neoforge/{build.version}/unix_args.txt"]
                if loader == "vanilla":
                    loader_version = None
            except versions.VersionError as e:
                shutil.rmtree(tmp, ignore_errors=True)
                raise ServerError(str(e))
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise

            properties.write(tmp / "server.properties", {
                **MANAGED_PROPERTIES, "server-port": str(self.cfg.server_port)})
            st = InstallState(
                minecraft_version=mc.id, loader=loader, loader_version=loader_version,
                java_major=mc.java_major, launch=launch, installed_at=time.time(),
                installed_by=requested_by,
                memory_mb=memory_mb or self.cfg.memory_mb)
            (tmp / "autoadmin.json").write_text(json.dumps(asdict(st), indent=2))
            self._write_eula_txt(tmp)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(tmp, target)
            became_active = self.active_world() is None
            if became_active:
                self._active_file.write_text(world)

        desc = f"Minecraft {mc.id}" + (f" with {loader} {loader_version}" if loader_version else " (vanilla)")
        notes = [f"Installed world '{world}': {desc}, Java {mc.java_major}."]
        notes.append("It is now the active world." if became_active
                     else "Switch to it to use it.")
        if not self.eula_record():
            notes.append("EULA not yet accepted.")
        return " ".join(notes)

    async def _run_neoforge_installer(self, java: Path, build: versions.NeoForgeBuild,
                                      target: Path) -> None:
        installer = await self.cache.fetch(build.installer_url, algo="sha1",
                                           digest=build.installer_sha1)
        log_path = self.jail.path("logs", f"install-neoforge-{build.version}-{int(time.time())}.log")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as log:
            proc = await asyncio.create_subprocess_exec(
                str(java), "-jar", str(installer), "--installServer", str(target),
                cwd=target, stdout=log, stderr=asyncio.subprocess.STDOUT)
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
        """Acceptance is recorded once and applied to every world."""
        self._eula_file.write_text(json.dumps({"accepted_by": accepted_by,
                                               "accepted_at": time.time()}))
        for d in self.worlds_root.glob("*/autoadmin.json") if self.worlds_root.exists() else []:
            self._write_eula_txt(d.parent)
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

    # -- desired state (read by the supervisor) ---------------------------------------

    @property
    def desired(self) -> str:
        """'running' after a successful start, 'stopped' after a stop."""
        try:
            return self._desired_file.read_text().strip() or "stopped"
        except FileNotFoundError:
            return "stopped"

    def set_desired(self, value: str) -> None:
        self._desired_file.write_text(value)

    @property
    def busy(self) -> bool:
        """True while a start/stop/install holds the lock (not a crash, whatever it looks like)."""
        return self._lock.locked()

    def crash_incident(self, unit: ProcessState) -> Incident:
        why = {"oom-kill": "killed for exceeding its memory cap (systemd MemoryMax)",
               "signal": "killed by a signal", "core-dump": "dumped core",
               "exit-code": (f"was killed by signal {unit.exit_status - 128}"
                             if (unit.exit_status or 0) > 128
                             else f"exited with status {unit.exit_status}")}.get(
            unit.result, f"stopped unexpectedly ({unit.result or 'unknown'})")
        st = self.state()
        summary = (f"Server crashed: {why}. World '{self.active_world()}', heap "
                   f"{st.memory_mb if st else '?'} MB.")
        return self._incident("crash", summary, read_lines(self.dir / "console.log"))

    # -- lifecycle -------------------------------------------------------------------

    async def start(self, progress: Progress = _noop) -> tuple[StartOutcome, str]:
        async with self._lock:
            st = self._require_installed()
            if await self.running():
                return StartOutcome.ALREADY_RUNNING, "The server is already running."
            java = self._java_bin(st.java_major) or await self.ensure_java(st.java_major, progress)
            # The console is AutoAdmin's own channel; never a network port.
            properties.write(self.dir / "server.properties", MANAGED_PROPERTIES)
            console = self.dir / "console.log"
            console.write_text("")   # so a previous run's "Done" can't be mistaken for this one
            started = time.time()
            await self.backend.start(LaunchSpec(
                workdir=self.dir, java=java, heap_mb=st.memory_mb, launch=st.launch,
                memory_max_mb=st.memory_mb + self.cfg.memory_overhead_mb,
                nice=self.cfg.nice, console_log=console))
            await progress("Starting the server…")
            return await self._await_startup(console, started)

    async def _await_startup(self, console: Path, started: float) -> tuple[StartOutcome, str]:
        deadline = started + self.cfg.start_timeout_s
        while True:
            await asyncio.sleep(1)
            lines = read_lines(console)
            if any(DONE.search(l) for l in lines):
                self.set_desired("running")
                return StartOutcome.READY, f"Server is up ({time.time() - started:.0f}s)."
            state = await self.backend.state()
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
            self._require_installed()
            self.set_desired("stopped")   # before stopping, so it never looks like a crash
            if not await self.running():
                return "The server isn't running."
            # The launcher turns this into a `stop` command, so the world is saved.
            await self.backend.stop()
            if (await self.backend.state()).result == "timeout":
                return "The server didn't stop cleanly in time, so I forced it."
            return "Server stopped."

    async def command(self, command: str, *, until: str | None = None,
                      timeout: float = 2.0) -> str:
        """Run a console command on the running server; returns its output."""
        try:
            lines = await self.backend.command(command, until=until, timeout=timeout)
        except ConsoleError as e:
            raise ServerError(str(e))
        return "\n".join(lines)

    async def console(self, command: str) -> str:
        self._require_installed()
        if not await self.running():
            raise ServerError("The server isn't running.")
        return await self.command(command) or "(no output)"

    async def status(self) -> dict:
        st = self.state()
        if st is None:
            return {"installed": False}
        unit = await self.backend.state()
        info = {
            "installed": True,
            "world": self.active_world(),
            "minecraft_version": st.minecraft_version,
            "loader": st.loader if not st.loader_version else f"{st.loader} {st.loader_version}",
            "java": st.java_major,
            "memory_mb": st.memory_mb,
            "eula_accepted": self.eula_record() is not None,
            "mods": len(st.mods),
            "state": "running" if unit.running else "stopped",
            "survives_logout": await self.backend.survives_logout(),
        }
        if unit.running:
            try:
                info["players"] = await self.command("list")
            except ServerError:
                info["players"] = "unknown (server still starting?)"
        return info


class InstallFailed(ServerError):
    def __init__(self, incident: Incident):
        super().__init__(incident.brief())
        self.incident = incident
