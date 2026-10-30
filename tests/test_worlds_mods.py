from __future__ import annotations

import json
import shutil
import tarfile
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from autoadmin.auth import Perm
from autoadmin.server.downloads import NotFound
from autoadmin.server.manager import InstallState, ServerError, ServerManager
from autoadmin.server.mods import inspect_jar
from autoadmin.server.systemd import UnitState
from autoadmin.services import Services
from autoadmin.tools import ToolContext, build_registry

from .conftest import ADMIN, OWNER, USER


# -- helpers -------------------------------------------------------------------------

class StoppedUnit:
    async def state(self):
        return UnitState("inactive", None, "")


def make_world(services, name="world", loader="fabric", mc="1.21.1", activate=True):
    d = services.server.worlds_root / name
    (d / "world" / "region").mkdir(parents=True, exist_ok=True)
    (d / "world" / "region" / "r.0.0.mca").write_bytes(b"chunks")
    (d / "libraries").mkdir(exist_ok=True)
    (d / "libraries" / "big.jar").write_bytes(b"x" * 1000)
    (d / "server.properties").write_text("level-name=world\nrcon.password=pw\n")
    services.server.save_state(InstallState(
        minecraft_version=mc, loader=loader, loader_version="0.1", java_major=21,
        launch=["-jar", "x.jar"], installed_at=0, installed_by="t", rcon_password="pw",
        memory_mb=2048), name)
    if activate:
        services.jail.path("active_world").write_text(name)
    return d


@pytest.fixture(autouse=True)
def stopped(services):
    services.server.unit = StoppedUnit()


def world_args(**kw):
    base = {"action": "list", "name": "", "minecraft_version": "", "loader": "",
            "backup_id": "", "count": 0}
    return {**base, **kw}


def mod_args(**kw):
    return {**{"action": "list", "query": "", "names": [], "url": ""}, **kw}


async def call(services, who, tool, args, turn=1, state=None, mentions=None):
    ctx = ToolContext(who, services, turn=turn, conv_state=state if state is not None else {},
                      mentions=mentions or {})
    return await build_registry().execute(ctx, tool, args)


# -- migration and world switching -----------------------------------------------------

def test_phase2_install_migrates_to_world(config):
    runtime = config.runtime
    (runtime / "server").mkdir(parents=True)
    (runtime / "server" / "autoadmin.json").write_text(json.dumps(dict(
        minecraft_version="1.20", loader="vanilla", loader_version=None, java_major=17,
        launch=["-jar", "server.jar"], installed_at=0, installed_by="o", rcon_password="pw",
        memory_mb=6144, eula_accepted_by="owner(cli-owner)", eula_accepted_at=1.0, extra={})))
    s = Services.from_config(config)
    assert s.server.active_world() == "world"
    assert s.server.state().minecraft_version == "1.20"
    assert s.server.eula_record()["accepted_by"] == "owner(cli-owner)"
    assert not (runtime / "server").exists()


async def test_list_and_switch(services):
    make_world(services, "alpha")
    make_world(services, "beta", loader="neoforge", activate=False)
    out, _ = await call(services, USER, "worlds", world_args())
    names = {w["name"]: w for w in json.loads(out)["worlds"]}
    assert names["alpha"]["active"] and not names["beta"]["active"]
    assert names["alpha"]["world_generated"]
    out, err = await call(services, USER, "worlds", world_args(action="switch", name="beta"))
    assert err and "worlds.manage" in out
    out, err = await call(services, ADMIN, "worlds", world_args(action="switch", name="beta"))
    assert not err and services.server.active_world() == "beta"
    out, err = await call(services, ADMIN, "worlds", world_args(action="switch", name="Bad Name"))
    assert err and "lowercase" in out


# -- confirmations -----------------------------------------------------------------------

async def test_delete_world_needs_confirmation_from_same_person_later(services):
    make_world(services, "alpha")
    make_world(services, "doomed", activate=False)
    state = {}
    args = world_args(action="delete", name="doomed")
    out, err = await call(services, ADMIN, "worlds", args, turn=1, state=state)
    assert not err and "CONFIRMATION NEEDED" in out
    out, _ = await call(services, ADMIN, "worlds", args, turn=1, state=state)   # same turn
    assert "CONFIRMATION NEEDED" in out
    out, _ = await call(services, OWNER, "worlds", args, turn=2, state=state)   # someone else
    assert "CONFIRMATION NEEDED" in out
    assert (services.server.worlds_root / "doomed").exists()
    out, err = await call(services, ADMIN, "worlds", args, turn=3, state=state)
    assert not err and "Deleted" in out and not (services.server.worlds_root / "doomed").exists()


# -- backups -------------------------------------------------------------------------------

async def test_backup_excludes_rebuildable_files_and_prunes_autos(services):
    make_world(services)
    b = services.backups
    manual = await b.create("world", "manual", "requested", "t")
    archive = b.root / "world" / f"{manual['id']}.tar.zst"
    with tarfile.open(archive, "r:zst") as tf:
        names = tf.getnames()
    assert "world/region/r.0.0.mca" in names and "server.properties" in names
    assert not any(n.startswith("libraries") for n in names)

    for i in range(5):
        await b.create("world", "auto", f"r{i}", "t")
    kinds = [x["kind"] for x in b.list("world")]
    assert kinds.count("auto") == 3 and kinds.count("manual") == 1
    b.set_retention(1)
    assert [x["kind"] for x in b.list("world")].count("auto") == 1


async def test_retention_is_owner_only(services):
    out, err = await call(services, ADMIN, "worlds", world_args(action="set_backup_retention", count=5))
    assert err and "admins.manage" in out
    out, err = await call(services, OWNER, "worlds", world_args(action="set_backup_retention", count=5))
    assert not err and services.backups.retention == 5


async def test_restore_existing_world_backs_up_first(services):
    d = make_world(services)
    meta = await services.backups.create("world", "manual", "requested", "t")
    (d / "world" / "region" / "r.0.0.mca").write_bytes(b"griefed")
    (d / "world" / "new.dat").write_bytes(b"later")
    state = {}
    args = world_args(action="restore", name="world", backup_id=meta["id"])
    out, _ = await call(services, ADMIN, "worlds", args, turn=1, state=state)
    assert "CONFIRMATION NEEDED" in out
    out, err = await call(services, ADMIN, "worlds", args, turn=2, state=state)
    assert not err and "Restored" in out
    assert (d / "world" / "region" / "r.0.0.mca").read_bytes() == b"chunks"
    assert not (d / "world" / "new.dat").exists()
    assert (d / "libraries" / "big.jar").exists()               # not touched
    assert any("before restoring" in x["reason"] for x in services.backups.list("world"))


async def test_restore_deleted_world_rebuilds_install(services, monkeypatch):
    d = make_world(services)
    meta = await services.backups.create("world", "manual", "requested", "t")
    shutil.rmtree(d)
    calls = []

    async def fake_install(self, mc, loader, by, progress=None, *, world, loader_version=None,
                           memory_mb=None):
        calls.append((mc, loader, world, loader_version, memory_mb))
        (self.worlds_root / world / "libraries").mkdir(parents=True)
        (self.worlds_root / world / "autoadmin.json").write_text("{}")
        return "installed"
    monkeypatch.setattr(ServerManager, "install", fake_install)
    msg = await services.backups.restore("world", meta["id"], "t")
    assert calls == [("1.21.1", "fabric", "world", "0.1", 2048)]
    assert (d / "world" / "region" / "r.0.0.mca").read_bytes() == b"chunks"
    assert json.loads((d / "autoadmin.json").read_text())["minecraft_version"] == "1.21.1"
    assert "Restored" in msg


# -- mods ------------------------------------------------------------------------------------

def version(vid, number, *deps, vtype="release"):
    return {"id": vid, "version_number": number, "version_type": vtype,
            "dependencies": [{"project_id": p, "dependency_type": t} for p, t in deps],
            "files": [{"primary": True, "url": f"https://cdn/{vid}.jar",
                       "filename": f"{vid}.jar", "hashes": {"sha512": "ab" * 64}}]}


PROJECTS = {
    "modmenu": {"id": "P_MM", "slug": "modmenu", "title": "Mod Menu", "project_type": "mod",
                "server_side": "optional", "game_versions": ["1.21.1"]},
    "P_API": {"id": "P_API", "slug": "fabric-api", "title": "Fabric API", "project_type": "mod",
              "server_side": "optional", "game_versions": ["1.21.1"]},
    "sodium": {"id": "P_SOD", "slug": "sodium", "title": "Sodium", "project_type": "mod",
               "server_side": "unsupported", "game_versions": ["1.21.1"]},
    "oldmod": {"id": "P_OLD", "slug": "oldmod", "title": "Old Mod", "project_type": "mod",
               "server_side": "required", "game_versions": ["1.16.5", "1.17"]},
    "clash": {"id": "P_CL", "slug": "clash", "title": "Clash", "project_type": "mod",
              "server_side": "required", "game_versions": ["1.21.1"]},
}
PROJECTS["P_API"] = PROJECTS["P_API"]
VERSIONS = {
    "P_MM": [version("mm2", "11.0.5", ("P_API", "required"), ("P_X", "optional"))],
    "P_API": [version("api9", "0.116.0", vtype="beta"), version("api8", "0.115.0")],
    "P_OLD": [],
    "P_CL": [version("cl1", "1.0", ("P_MM", "incompatible"))],
}


@pytest.fixture
def modrinth(monkeypatch, services):
    async def fake_get_json(url):
        path = urllib.parse.urlparse(url).path
        parts = path.split("/")
        if parts[-1] == "version":
            return VERSIONS[parts[-2]]
        ident = urllib.parse.unquote(parts[-1])
        if ident not in PROJECTS:
            raise NotFound("404")
        return PROJECTS[ident]
    monkeypatch.setattr("autoadmin.server.mods.get_json", fake_get_json)

    jars = services.jail.path("fakejars")
    jars.mkdir(exist_ok=True)

    async def fake_fetch(url, **kw):
        vid = url.rsplit("/", 1)[-1].removesuffix(".jar")
        mod_id = {"mm2": "modmenu", "api8": "fabric-api", "cl1": "clash"}.get(vid, vid)
        return _jar(jars / f"{vid}.jar", "fabric.mod.json", json.dumps({"id": mod_id, "version": "1"}))
    monkeypatch.setattr(services.server.cache, "fetch", fake_fetch)


async def test_install_with_dependency_and_bounces(services, modrinth):
    make_world(services)
    out, err = await call(services, ADMIN, "mods",
                          mod_args(action="install", names=["modmenu", "sodium", "nope", "oldmod"]))
    assert not err
    lines = out.splitlines()
    assert lines[0].startswith("OK   Mod Menu 11.0.5")
    assert lines[1] == "OK   Fabric API (needed by modmenu) 0.115.0 -> api8.jar"   # release preferred
    assert lines[2] == "SKIP Sodium: client-only, so players install it themselves; not added"
    assert lines[3] == "FAIL nope: not found on Modrinth"
    assert lines[4].startswith("FAIL Old Mod: no fabric build for Minecraft 1.21.1") and "1.17" in lines[4]
    mods_dir = services.server.dir / "mods"
    assert {p.name for p in mods_dir.glob("*.jar")} == {"mm2.jar", "api8.jar"}
    st = services.server.state()
    api = next(m for m in st.mods if m["slug"] == "fabric-api")
    assert api["required_by"] == ["modmenu"]
    assert any("before installing" in b["reason"] for b in services.backups.list("world"))

    out, _ = await call(services, ADMIN, "mods", mod_args(action="install", names=["clash", "modmenu"]))
    assert "incompatible with installed Mod Menu" in out and "already installed" in out


async def test_remove_respects_dependencies_and_disable_moves_jar(services, modrinth):
    make_world(services)
    await call(services, ADMIN, "mods", mod_args(action="install", names=["modmenu"]))
    out, _ = await call(services, ADMIN, "mods", mod_args(action="remove", names=["fabric-api"]))
    assert "still required by modmenu" in out
    out, _ = await call(services, ADMIN, "mods", mod_args(action="disable", names=["Mod Menu"]))
    mods_dir = services.server.dir / "mods"
    assert "disabled Mod Menu" in out and (mods_dir / "disabled" / "mm2.jar").exists()
    await call(services, ADMIN, "mods", mod_args(action="enable", names=["modmenu"]))
    assert (mods_dir / "mm2.jar").exists()
    out, _ = await call(services, ADMIN, "mods", mod_args(action="remove",
                                                         names=["modmenu", "fabric-api"]))
    assert out.count("OK   removed") == 2 and not list(mods_dir.glob("*.jar"))


async def test_mods_need_modded_world_and_permission(services, modrinth):
    make_world(services, loader="vanilla")
    out, err = await call(services, ADMIN, "mods", mod_args(action="list"))
    assert err and "vanilla" in out
    out, err = await call(services, USER, "mods", mod_args(action="install", names=["x"]))
    assert err and "mods.manage" in out


def _jar(path: Path, name: str, body: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, body)
    return path


def test_inspect_jar(tmp_path):
    fab = inspect_jar(_jar(tmp_path / "a.jar", "fabric.mod.json",
                           '{"id": "zoom", "version": "1.0", "environment": "client"}'))
    assert fab.loader == "fabric" and fab.client_only
    neo = inspect_jar(_jar(tmp_path / "b.jar", "META-INF/neoforge.mods.toml",
                           '[[mods]]\nmodId="create"\nversion="6.0"\n'))
    assert (neo.loader, neo.mod_id, neo.client_only) == ("neoforge", "create", False)
    with pytest.raises(ServerError, match="not a Fabric/NeoForge"):
        inspect_jar(_jar(tmp_path / "c.jar", "plugin.yml", "x"))


async def test_install_upload_checks_uploader(services, monkeypatch, tmp_path):
    make_world(services)
    jar = _jar(tmp_path / "cool.jar", "fabric.mod.json", '{"id": "cool", "version": "2.0"}')

    async def fake_fetch(url, **kw):
        return jar
    monkeypatch.setattr(services.server.cache, "fetch", fake_fetch)
    state = {"attachments": {"cool.jar": {"url": "https://cdn/cool.jar", "by": USER.id, "size": 9}}}
    args = mod_args(action="install_upload", names=["cool.jar"])
    out, err = await call(services, ADMIN, "mods", args, state=state)
    assert err and "lacks mods.manage" in out
    services.perms.grant(USER.id, USER.name, Perm.MODS_MANAGE)
    out, err = await call(services, ADMIN, "mods", args, state=state)
    assert not err and "cool 2.0 -> cool.jar" in out
    assert (services.server.dir / "mods" / "cool.jar").exists()
    out, err = await call(services, ADMIN, "mods", args, state=state)
    assert err and "already installed" in out


async def test_dependency_already_uploaded_is_not_duplicated(services, modrinth, monkeypatch, tmp_path):
    """Regression: an uploaded jar later pulled in as a Modrinth dependency under another name."""
    make_world(services)
    services.perms.grant(ADMIN.id, ADMIN.name, Perm.MODS_MANAGE)
    jar = _jar(tmp_path / "fabric_api_upload.jar", "fabric.mod.json", '{"id": "fabric-api", "version": "1"}')
    modrinth_fetch = services.server.cache.fetch

    async def upload_fetch(url, **kw):
        return jar if url == "https://cdn/up.jar" else await modrinth_fetch(url, **kw)
    monkeypatch.setattr(services.server.cache, "fetch", upload_fetch)
    state = {"attachments": {"fabric_api_upload.jar": {"url": "https://cdn/up.jar", "by": ADMIN.id, "size": 1}}}
    out, err = await call(services, ADMIN, "mods",
                          mod_args(action="install_upload", names=["fabric_api_upload.jar"]), state=state)
    assert not err
    out, err = await call(services, ADMIN, "mods", mod_args(action="install", names=["modmenu"]))
    assert "OK   Mod Menu" in out
    assert "SKIP Fabric API (needed by modmenu): already installed as fabric_api_upload.jar" in out
    st = services.server.state()
    assert len(st.mods) == 2
    uploaded = next(m for m in st.mods if m["filename"] == "fabric_api_upload.jar")
    assert uploaded["required_by"] == ["modmenu"]
    assert sorted(p.name for p in (services.server.dir / "mods").glob("*.jar")) == \
        ["fabric_api_upload.jar", "mm2.jar"]
