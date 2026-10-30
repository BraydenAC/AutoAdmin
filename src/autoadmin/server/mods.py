"""Mods for the active world: Modrinth installs, URL/upload fallbacks, enable/disable.

The agent searches and picks exact slugs. Everything after that (version
choice, dependencies, client-only checks, download, checksum) happens here,
and the agent gets one line per mod back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tomllib
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .backups import BackupStore
from .downloads import DownloadError, NotFound, get_json
from .manager import InstallState, ServerError, ServerManager, _noop, Progress

MODRINTH = "https://api.modrinth.com/v2"
MOD_LOADERS = ("fabric", "neoforge")
MAX_DEPENDENCY_DEPTH = 6


def _q(value: object) -> str:
    return urllib.parse.quote(json.dumps(value))


@dataclass
class InstallReport:
    lines: list[str] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.lines.append(f"OK   {text}")

    def skip(self, text: str) -> None:
        self.lines.append(f"SKIP {text}")

    def fail(self, text: str) -> None:
        self.lines.append(f"FAIL {text}")

    def __str__(self) -> str:
        return "\n".join(self.lines) or "Nothing to do."


@dataclass
class JarInfo:
    loader: str            # fabric | neoforge
    mod_id: str
    version: str
    client_only: bool


def inspect_jar(path: Path) -> JarInfo:
    """Read loader metadata from a mod jar, or raise ServerError if it isn't one."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                meta = json.loads(z.read("fabric.mod.json").decode(errors="replace"), strict=False)
                return JarInfo("fabric", str(meta.get("id", "?")), str(meta.get("version", "?")),
                               meta.get("environment") == "client")
            if "META-INF/neoforge.mods.toml" in names:
                meta = tomllib.loads(z.read("META-INF/neoforge.mods.toml").decode(errors="replace"))
                mod = (meta.get("mods") or [{}])[0]
                # NeoForge has no reliable client-only marker.
                return JarInfo("neoforge", str(mod.get("modId", "?")),
                               str(mod.get("version", "?")), False)
    except (zipfile.BadZipFile, ValueError, tomllib.TOMLDecodeError) as e:
        raise ServerError(f"Not a valid mod jar: {e}")
    raise ServerError("No fabric.mod.json or neoforge.mods.toml in the jar; not a Fabric/NeoForge mod.")


def _sha512(path: Path) -> str:
    h = hashlib.sha512()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class ModManager:
    def __init__(self, server: ServerManager, backups: BackupStore):
        self.server = server
        self.backups = backups

    # -- helpers -------------------------------------------------------------------

    def _modded(self) -> InstallState:
        st = self.server._require_installed()
        if st.loader not in MOD_LOADERS:
            raise ServerError(f"World '{self.server.active_world()}' is vanilla; mods need "
                              "a Fabric or NeoForge world.")
        return st

    @property
    def mods_dir(self) -> Path:
        return self.server.dir / "mods"

    def _find(self, st: InstallState, name: str) -> dict | None:
        n = name.lower().removesuffix(".jar")
        for m in st.mods:
            if n in {str(m.get("slug", "")).lower(), str(m.get("title", "")).lower(),
                     m["filename"].lower().removesuffix(".jar")}:
                return m
        return None

    @staticmethod
    def _jar_mod_id(path: Path) -> str | None:
        try:
            return inspect_jar(path).mod_id
        except ServerError:
            return None

    @staticmethod
    def _same_mod(st: InstallState, filename: str, mod_id: str | None) -> dict | None:
        """An installed mod that is the same jar or declares the same mod id."""
        for m in st.mods:
            if m["filename"] == filename:
                return m
            if mod_id and mod_id in (m.get("mod_id"), m.get("slug")):
                return m
        return None

    def _path(self, m: dict) -> Path:
        return self.mods_dir / ("" if m.get("enabled", True) else "disabled") / m["filename"]

    async def _backup(self, reason: str, by: str) -> None:
        await self.backups.create(self.server.active_world(), "auto", reason, by)

    async def _restart_note(self) -> str:
        return " Restart the server to apply." if await self.server.running() else ""

    # -- search & list -------------------------------------------------------------------

    async def search(self, query: str, limit: int = 6) -> list[dict]:
        st = self._modded()
        facets = [[f"categories:{st.loader}"], [f"versions:{st.minecraft_version}"],
                  ["project_type:mod"]]
        url = (f"{MODRINTH}/search?query={urllib.parse.quote(query)}"
               f"&facets={_q(facets)}&limit={limit}")
        try:
            hits = (await get_json(url))["hits"]
        except DownloadError as e:
            raise ServerError(f"Modrinth search failed: {e}")
        return [{
            "slug": h["slug"], "title": h["title"],
            "description": h["description"][:140],
            "downloads": h["downloads"],
            "client_only": h.get("server_side") == "unsupported",
        } for h in hits]

    def list(self) -> list[dict]:
        st = self._modded()
        return [{"name": m.get("title") or m.get("slug"), "slug": m.get("slug"),
                 "version": m.get("version"), "file": m["filename"],
                 "enabled": m.get("enabled", True), "source": m["source"],
                 "required_by": m.get("required_by", [])} for m in st.mods]

    # -- Modrinth install -------------------------------------------------------------------

    async def install(self, slugs: list[str], by: str, progress: Progress = _noop) -> str:
        st = self._modded()
        if not slugs:
            raise ServerError("Name at least one mod slug.")
        await self._backup(f"before installing {', '.join(slugs)}", by)
        self.mods_dir.mkdir(exist_ok=True)
        report = InstallReport()
        await progress(f"Installing {len(slugs)} mod{'s' if len(slugs) > 1 else ''} from Modrinth…")
        for slug in slugs:
            await self._install_one(st, slug, None, report, depth=0)
        self.server.save_state(st)
        return str(report) + await self._restart_note()

    async def _install_one(self, st: InstallState, ident: str, required_by: str | None,
                           report: InstallReport, depth: int) -> None:
        label = ident if required_by is None else f"{ident} (needed by {required_by})"
        try:
            project = await get_json(f"{MODRINTH}/project/{urllib.parse.quote(ident)}")
        except NotFound:
            report.fail(f"{label}: not found on Modrinth")
            return
        except DownloadError as e:
            report.fail(f"{label}: Modrinth unavailable ({e})")
            return
        title, slug, pid = project["title"], project["slug"], project["id"]
        label = title if required_by is None else f"{title} (needed by {required_by})"

        existing = next((m for m in st.mods if m.get("project_id") == pid), None)
        if existing:
            if required_by and required_by not in existing.setdefault("required_by", []):
                existing["required_by"].append(required_by)
            if required_by is None:
                report.skip(f"{title}: already installed ({existing.get('version')})")
            return
        if project.get("project_type") != "mod":
            report.fail(f"{label}: is a {project.get('project_type')}, not a mod")
            return
        if project.get("server_side") == "unsupported":
            report.skip(f"{label}: client-only, so players install it themselves; not added")
            return

        try:
            versions = await get_json(
                f"{MODRINTH}/project/{pid}/version?loaders={_q([st.loader])}"
                f"&game_versions={_q([st.minecraft_version])}")
        except DownloadError as e:
            report.fail(f"{label}: couldn't list versions ({e})")
            return
        if not versions:
            recent = ", ".join(project.get("game_versions", [])[-3:]) or "none"
            report.fail(f"{label}: no {st.loader} build for Minecraft {st.minecraft_version} "
                        f"(latest supported: {recent})")
            return
        version = next((v for v in versions if v["version_type"] == "release"), versions[0])

        installed_ids = {m.get("project_id") for m in st.mods}
        clash = [d for d in version.get("dependencies", [])
                 if d["dependency_type"] == "incompatible" and d.get("project_id") in installed_ids]
        if clash:
            names = [m.get("title") for m in st.mods if m.get("project_id") in
                     {d["project_id"] for d in clash}]
            report.fail(f"{label}: incompatible with installed {', '.join(names)}")
            return

        file = next((f for f in version["files"] if f.get("primary")), version["files"][0])
        try:
            cached = await self.server.cache.fetch(file["url"], algo="sha512",
                                                   digest=file["hashes"]["sha512"])
        except DownloadError as e:
            report.fail(f"{label}: download failed ({e}); try a direct URL or upload the jar")
            return
        mod_id = self._jar_mod_id(cached)
        same = self._same_mod(st, file["filename"], mod_id)
        if same:
            # Already present under another identity (e.g. uploaded earlier).
            if required_by and required_by not in same.setdefault("required_by", []):
                same["required_by"].append(required_by)
            report.skip(f"{label}: already installed as {same['filename']}")
            return
        await asyncio.to_thread(shutil.copyfile, cached, self.mods_dir / file["filename"])

        st.mods.append({
            "project_id": pid, "slug": slug, "title": title, "mod_id": mod_id,
            "version_id": version["id"], "version": version["version_number"],
            "filename": file["filename"], "sha512": file["hashes"]["sha512"],
            "source": "modrinth", "enabled": True,
            "required_by": [required_by] if required_by else [],
        })
        tag = "" if version["version_type"] == "release" else f" [{version['version_type']}]"
        report.ok(f"{label} {version['version_number']}{tag} -> {file['filename']}")

        if depth >= MAX_DEPENDENCY_DEPTH:
            return
        for dep in version.get("dependencies", []):
            if dep["dependency_type"] == "required" and dep.get("project_id"):
                await self._install_one(st, dep["project_id"], slug, report, depth + 1)

    # -- URL / upload fallbacks -------------------------------------------------------------

    async def install_file(self, url: str, filename: str, source: str, by: str) -> str:
        st = self._modded()
        filename = Path(urllib.parse.unquote(filename)).name
        if not filename.endswith(".jar"):
            raise ServerError("Only .jar files can be installed as mods.")
        try:
            cached = await self.server.cache.fetch(url)
        except DownloadError as e:
            raise ServerError(f"Couldn't download {filename}: {e}")
        info = await asyncio.to_thread(inspect_jar, cached)
        if info.loader != st.loader:
            raise ServerError(f"{filename} is a {info.loader} mod but this world runs {st.loader}.")
        if info.client_only:
            raise ServerError(f"{filename} is client-only, so players install it themselves; not added.")
        same = self._same_mod(st, filename, info.mod_id) or self._find(st, info.mod_id)
        if same:
            raise ServerError(f"{info.mod_id} is already installed as {same['filename']}.")
        await self._backup(f"before installing {filename}", by)
        self.mods_dir.mkdir(exist_ok=True)
        dest = self.mods_dir / filename
        await asyncio.to_thread(shutil.copyfile, cached, dest)
        st.mods.append({
            "project_id": None, "slug": info.mod_id, "title": info.mod_id, "mod_id": info.mod_id,
            "version": info.version, "filename": filename, "sha512": _sha512(dest),
            "source": source, "enabled": True, "required_by": [],
        })
        self.server.save_state(st)
        return (f"OK   {info.mod_id} {info.version} -> {filename} (from {source}; "
                "dependencies are not checked for jars outside Modrinth)" + await self._restart_note())

    # -- remove / disable / enable -----------------------------------------------------------

    async def remove(self, names: list[str], by: str) -> str:
        st = self._modded()
        targets, report = [], InstallReport()
        for n in names:
            m = self._find(st, n)
            if m is None:
                report.fail(f"{n}: not installed")
            else:
                targets.append(m)
        removing = {m.get("slug") for m in targets}
        for m in list(targets):
            needed_by = [r for r in m.get("required_by", []) if r not in removing]
            if needed_by:
                report.fail(f"{m.get('title')}: still required by {', '.join(needed_by)}")
                targets.remove(m)
        if targets:
            await self._backup(f"before removing {', '.join(m.get('slug') for m in targets)}", by)
        for m in targets:
            self._path(m).unlink(missing_ok=True)
            st.mods.remove(m)
            for other in st.mods:
                if m.get("slug") in other.get("required_by", []):
                    other["required_by"].remove(m.get("slug"))
            report.ok(f"removed {m.get('title')} ({m['filename']})")
        self.server.save_state(st)
        return str(report) + (await self._restart_note() if targets else "")

    async def set_enabled(self, names: list[str], enabled: bool) -> str:
        """Disabling moves the jar to mods/disabled/, which loaders don't scan."""
        st = self._modded()
        report = InstallReport()
        (self.mods_dir / "disabled").mkdir(parents=True, exist_ok=True)
        for n in names:
            m = self._find(st, n)
            if m is None:
                report.fail(f"{n}: not installed")
                continue
            if m.get("enabled", True) == enabled:
                report.skip(f"{m.get('title')}: already {'enabled' if enabled else 'disabled'}")
                continue
            src = self._path(m)
            m["enabled"] = enabled
            dst = self._path(m)
            if src.exists():
                src.replace(dst)
            report.ok(f"{'enabled' if enabled else 'disabled'} {m.get('title')}")
        self.server.save_state(st)
        return str(report) + await self._restart_note()
