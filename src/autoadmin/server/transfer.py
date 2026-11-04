"""Moving worlds between machines through the Downloads folder.

`export_world` writes the zip `autoadmin uninstall` leaves in Downloads: the
world's folder (save, settings, mods, autoadmin.json) without the server jars and
libraries, which are re-downloadable and platform-specific.

`import_world` rebuilds a world from such a zip: a fresh server install for this
machine, then the save, settings and mods on top. The zip is found only by its exact
full file name in Downloads; nothing there is searched or listed.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from . import properties
from .backups import _included
from .manager import MANAGED_PROPERTIES, Progress, ServerError, ServerManager, _noop

SAFE_NAME = re.compile(r"[\w.\- ()]{1,200}")
# Never taken from an import: this machine's own state, and the EULA, which a human
# here must accept.
SKIP_TOP = {"autoadmin.json", "eula.txt"}
DISK_MARGIN = 1 << 30


def downloads_dir() -> Path:
    home = Path.home()
    if sys.platform.startswith("linux") and shutil.which("xdg-user-dir"):
        out = subprocess.run(["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True)
        path = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
        if path and path != home and path.is_dir():
            return path
    return home / "Downloads" if (home / "Downloads").is_dir() else home


# -- export ----------------------------------------------------------------------------------

def export_world(world_dir: Path, dest: Path) -> Path:
    """Zip the world folder. Written to a temp name first, so a partial zip never
    looks finished."""
    tmp = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for top in _included(world_dir):
            for f in [top] if top.is_file() else sorted(top.rglob("*")):
                if f.is_file() and not f.is_symlink() and f.name != "session.lock":
                    zf.write(f, Path(world_dir.name) / f.relative_to(world_dir))
    tmp.replace(dest)
    return dest


# -- finding an import -------------------------------------------------------------------------

def find_import(name: str, folder: Path) -> Path:
    """Only an exact, full zip file name matches: nothing in Downloads can be found or
    listed without already knowing its name."""
    if (not SAFE_NAME.fullmatch(name) or not name.endswith(".zip")
            or name.startswith(".")):
        raise ServerError("Give the backup zip's full file name, e.g. "
                          "AutoAdmin-survival-20261104-111500.zip (not a path).")
    path = folder / name
    # Exact spelling, also on case-insensitive filesystems (Windows).
    if not path.is_file() or path.is_symlink() or name not in {p.name for p in folder.iterdir()}:
        raise ServerError(f"There's no file named exactly '{name}' in the host's Downloads "
                          "folder.")
    return path


# -- reading an import -------------------------------------------------------------------------

class _Source:
    """A zip holding one world folder (the one with autoadmin.json)."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path)
        roots = {PurePosixPath(n).parent for n in self.zip.namelist()
                 if PurePosixPath(n).name == "autoadmin.json" and len(PurePosixPath(n).parts) <= 2}
        if len(roots) != 1:
            self.zip.close()
            raise ServerError(f"'{path.name}' isn't an AutoAdmin world backup "
                              "(no single world folder with autoadmin.json in it).")
        self.root = roots.pop()

    def close(self) -> None:
        self.zip.close()

    @property
    def world_name(self) -> str:
        return self.root.name or self.path.stem

    def state(self) -> dict:
        try:
            return json.loads(self.zip.read((self.root / "autoadmin.json").as_posix()))
        except ValueError as e:
            raise ServerError(f"The backup's autoadmin.json is unreadable: {e}")

    def files(self) -> list[tuple[PurePosixPath, int]]:
        """(path inside the world folder, size). Unsafe and skipped entries left out."""
        out = []
        for info in self.zip.infolist():
            path = PurePosixPath(info.filename)
            if info.is_dir() or path.is_absolute() or ".." in path.parts \
                    or not path.is_relative_to(self.root):
                continue
            rel = path.relative_to(self.root)
            if not rel.parts or rel.parts[0] in SKIP_TOP or rel.name == "session.lock":
                continue
            out.append((rel, info.file_size))
        return out

    def copy(self, rel: PurePosixPath, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.zip.open((self.root / rel).as_posix()) as r, dest.open("wb") as w:
            shutil.copyfileobj(r, w, 1 << 20)


# -- import -------------------------------------------------------------------------------------

async def import_world(server: ServerManager, source: Path, world: str | None, by: str,
                       progress: Progress = _noop) -> str:
    try:
        src = _Source(source)
    except zipfile.BadZipFile:
        raise ServerError(f"'{source.name}' isn't a readable zip file.")
    try:
        world = world or src.world_name
        target = server.world_dir(world)              # validates the name
        if (target / "autoadmin.json").exists():
            raise ServerError(f"A world named '{world}' already exists. Give the import "
                              "another name.")
        old = src.state()
        files = src.files()
        size = sum(s for _, s in files)
        free = shutil.disk_usage(server.worlds_root.parent).free
        if size + DISK_MARGIN > free:
            raise ServerError(f"Not enough disk space: the backup unpacks to "
                              f"{size / 1024 ** 3:.1f} GB.")
        try:
            mc, loader = old["minecraft_version"], old["loader"]
        except KeyError:
            raise ServerError("The backup's autoadmin.json has no Minecraft version or loader.")
        memory, note = _fit_memory(server, old.get("memory_mb"))

        await progress(f"Importing '{source.name}' as world '{world}': installing "
                       f"Minecraft {mc} ({loader}) for this machine first…")
        await server.install(mc, loader, by, progress, world=world,
                             loader_version=old.get("loader_version"), memory_mb=memory)
        try:
            await progress("Copying the world, settings and mods…")
            await asyncio.to_thread(_overlay, src, files, target)
            properties.write(target / "server.properties", {
                **MANAGED_PROPERTIES, "server-port": str(server.cfg.server_port)})
            st = server.state(world)
            st.mods = old.get("mods") or []
            st.extra = old.get("extra") or {}
            server.save_state(st, world)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)      # no half-imported world
            raise
    finally:
        src.close()
    notes = [f"Imported '{source.name}' as world '{world}' (Minecraft {mc}, {loader}, "
             f"{len(st.mods)} mods)."]
    if note:
        notes.append(note)
    if server.active_world() != world:
        notes.append("Switch to it to use it.")
    if not server.eula_record():
        notes.append("EULA not yet accepted.")
    return " ".join(notes)


def _overlay(src: _Source, files: list[tuple[PurePosixPath, int]], target: Path) -> None:
    # The fresh install's files that the backup also has (e.g. server.properties) are replaced.
    for rel, _ in files:
        src.copy(rel, target.joinpath(*rel.parts))


def _fit_memory(server: ServerManager, mb: int | None) -> tuple[int | None, str]:
    """The old machine may have had more memory than this one."""
    if not mb:
        return None, ""
    limit = server.host_memory_mb() - server.cfg.min_host_free_mb - server.cfg.memory_overhead_mb
    if mb <= limit:
        return mb, ""
    fitted = max(1024, min(server.cfg.memory_mb, limit))
    return fitted, f"Its memory setting ({mb} MB) was too big for this machine, so it's {fitted} MB."
