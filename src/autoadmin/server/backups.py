"""World backups: zstd archives of what can't be re-downloaded.

Server jars and libraries are left out; a restore into a missing world first
rebuilds the install from the profile stored in the backup.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import tarfile
import time
from pathlib import Path

from ..config import Jail
from .manager import ServerError, ServerManager, _noop, Progress
from .rcon import RconError

# Top-level entries in a world folder that are rebuilt, not backed up.
EXCLUDE = {
    "libraries", "versions", "logs", "crash-reports", "console.log", ".fabric",
    "server.jar", "fabric-server-launch.jar", "run.sh", "run.bat",
    "installer.jar", "installer.jar.log",
}
BACKUP_ID = re.compile(r"\d{8}-\d{6}-(auto|manual)(-\d+)?")
DEFAULT_RETENTION = 3


def _included(world_dir: Path) -> list[Path]:
    return sorted(p for p in world_dir.iterdir() if p.name not in EXCLUDE)


class Settings:
    """Small runtime settings file for values changed from chat."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def get(self, key: str, default):
        return self._read().get(key, default)

    def set(self, key: str, value) -> None:
        data = self._read()
        data[key] = value
        self.path.write_text(json.dumps(data, indent=2))


class BackupStore:
    def __init__(self, jail: Jail, server: ServerManager, settings: Settings):
        self.root = jail.path("backups")
        self.server = server
        self.settings = settings

    # -- retention -----------------------------------------------------------------

    @property
    def retention(self) -> int:
        return int(self.settings.get("backup_retention", DEFAULT_RETENTION))

    def set_retention(self, count: int) -> str:
        if not 1 <= count <= 50:
            raise ServerError("Keep between 1 and 50 automatic backups per world.")
        self.settings.set("backup_retention", count)
        for world_dir in self.root.glob("*"):
            self._prune(world_dir.name)
        return f"Keeping the newest {count} automatic backups per world."

    def _prune(self, world: str) -> list[str]:
        autos = [b for b in self.list(world) if b["kind"] == "auto"]
        removed = []
        for b in autos[self.retention:]:
            self._files(world, b["id"])[0].unlink(missing_ok=True)
            self._files(world, b["id"])[1].unlink(missing_ok=True)
            removed.append(b["id"])
        return removed

    # -- listing -------------------------------------------------------------------

    def _files(self, world: str, backup_id: str) -> tuple[Path, Path]:
        if not BACKUP_ID.fullmatch(backup_id):
            raise ServerError(f"'{backup_id}' is not a backup id.")
        d = self.root / world
        return d / f"{backup_id}.tar.zst", d / f"{backup_id}.json"

    def list(self, world: str) -> list[dict]:
        d = self.root / world
        metas = [json.loads(p.read_text()) for p in d.glob("*.json")] if d.exists() else []
        return sorted(metas, key=lambda m: m["created_at"], reverse=True)

    def get(self, world: str, backup_id: str) -> dict:
        _, meta = self._files(world, backup_id)
        if not meta.exists():
            raise ServerError(f"World '{world}' has no backup '{backup_id}'.")
        return json.loads(meta.read_text())

    # -- create --------------------------------------------------------------------

    async def create(self, world: str, kind: str, reason: str, by: str) -> dict:
        world_dir = self.server.world_dir(world)
        st = self.server.state(world)
        if st is None:
            raise ServerError(f"There is no world named '{world}'.")
        live = world == self.server.active_world() and await self.server.running()
        rcon = self.server._rcon(st) if live else None
        if rcon:
            # Flush and pause saving so the archive is consistent.
            try:
                await rcon.command("save-off")
                await rcon.command("save-all flush")
            except RconError as e:
                raise ServerError(f"Couldn't pause world saving for the backup: {e}")
        try:
            out_dir = self.root / world
            out_dir.mkdir(parents=True, exist_ok=True)
            backup_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}"
            n = 1
            while (out_dir / f"{backup_id}.json").exists():
                n += 1
                backup_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{n}"
            archive, meta_path = self._files(world, backup_id)

            def write() -> None:
                tmp = archive.with_name(archive.name + ".part")
                with tarfile.open(tmp, "w:zst") as tf:
                    for p in _included(world_dir):
                        tf.add(p, arcname=p.name)
                tmp.replace(archive)
            await asyncio.to_thread(write)
        finally:
            if rcon:
                try:
                    await rcon.command("save-on")
                except RconError:
                    pass

        meta = {
            "id": backup_id, "world": world, "kind": kind, "reason": reason,
            "created_by": by, "created_at": time.time(),
            "size_mb": round(archive.stat().st_size / 1024 / 1024, 1),
            "minecraft_version": st.minecraft_version, "loader": st.loader,
            "loader_version": st.loader_version, "memory_mb": st.memory_mb,
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        if kind == "auto":
            self._prune(world)
        return meta

    # -- restore / delete --------------------------------------------------------------

    async def restore(self, world: str, backup_id: str, by: str,
                      progress: Progress = _noop) -> str:
        meta = self.get(world, backup_id)
        archive, _ = self._files(world, backup_id)
        world_dir = self.server.world_dir(world)
        notes = []
        if (world_dir / "autoadmin.json").exists():
            if world == self.server.active_world() and await self.server.running():
                raise ServerError("Stop the server before restoring its active world.")
            safety = await self.create(world, "auto", f"before restoring {backup_id}", by)
            notes.append(f"Saved the current state first as {safety['id']}.")
        else:
            await progress(f"Rebuilding the '{world}' server install before restoring…")
            await self.server.install(meta["minecraft_version"], meta["loader"], by, progress,
                                      world=world, loader_version=meta["loader_version"],
                                      memory_mb=meta["memory_mb"])

        def swap() -> None:
            for p in _included(world_dir):
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            with tarfile.open(archive, "r:zst") as tf:
                tf.extractall(world_dir, filter="data")
        await asyncio.to_thread(swap)
        notes.insert(0, f"Restored '{world}' to backup {backup_id}.")
        return " ".join(notes)

    def delete(self, world: str, backup_id: str) -> str:
        self.get(world, backup_id)
        for p in self._files(world, backup_id):
            p.unlink(missing_ok=True)
        return f"Deleted backup {backup_id} of '{world}'."
