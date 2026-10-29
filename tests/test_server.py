from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from pathlib import Path

import pytest

from autoadmin.agent.investigator import Investigator
from autoadmin.auth import Perm
from autoadmin.server import properties
from autoadmin.server.downloads import Cache, DownloadError
from autoadmin.server.incidents import excerpt_failure, read_lines
from autoadmin.server.manager import InstallState, StartOutcome
from autoadmin.server.rcon import Rcon, RconError
from autoadmin.server.systemd import UnitState
from autoadmin.server.versions import VersionError, neoforge_prefix, pick_neoforge
from autoadmin.tools import ToolContext, ToolError, build_registry
from autoadmin.tools.server import console_permission

from .conftest import ADMIN, OWNER, USER, FakeClient, response, text, tool_use

FIXTURES = Path(__file__).parent / "fixtures"


# -- excerpts ------------------------------------------------------------------

def test_excerpt_of_real_neoforge_failure():
    lines = read_lines(FIXTURES / "neoforge-install-fail.log")
    ex = excerpt_failure(lines)
    assert "There was an error during installation" in ex
    assert "\tat net.minecraftforge.installer" in ex or "at net.minecraftforge.installer" in ex
    assert len(ex.splitlines()) <= 45


def test_excerpt_without_trace_is_tail():
    lines = [f"line {i}" for i in range(100)]
    ex = excerpt_failure(lines)
    assert ex.splitlines()[-1] == "line 99" and len(ex.splitlines()) == 15


def test_excerpt_trims_huge_trace():
    lines = ["boom: java.lang.RuntimeException: x"] + ["\tat a.b.C.d(C.java:1)"] * 300 + ["after"]
    ex = excerpt_failure(lines)
    assert "RuntimeException" in ex and "trace trimmed" in ex and ex.endswith("after")


# -- versions ------------------------------------------------------------------

@pytest.mark.parametrize("mc,prefix", [
    ("1.21.1", "21.1."), ("1.21", "21.0."), ("1.20.4", "20.4."),
    ("26.3", "26.3.0."), ("26.1.2", "26.1.2."),
])
def test_neoforge_prefix(mc, prefix):
    assert neoforge_prefix(mc) == prefix


def test_pick_neoforge_prefers_newest_stable():
    vs = ["21.1.9", "21.1.265", "21.1.100", "21.1.266-beta", "21.10.1", "26.3.0.99"]
    assert pick_neoforge(vs, "1.21.1") == "21.1.265"
    assert pick_neoforge(["21.1.1-beta"], "1.21.1") == "21.1.1-beta"
    with pytest.raises(VersionError):
        pick_neoforge(vs, "1.19.2")


# -- properties ------------------------------------------------------------------

def test_properties_validate_and_merge(tmp_path):
    p = tmp_path / "server.properties"
    p.write_text("#c\nrcon.password=secret\nmotd=old\n")
    clean = properties.validate({"level-type": "superflat", "difficulty": "hard", "max-players": "10"})
    assert clean["level-type"] == "minecraft:flat"
    properties.write(p, clean)
    got = properties.read(p)
    assert got["rcon.password"] == "secret" and got["difficulty"] == "hard"
    with pytest.raises(ValueError) as e:
        properties.validate({"rcon.password": "x", "difficulty": "brutal", "view-distance": "99"})
    assert "rcon.password: not editable" in str(e.value)
    assert "difficulty" in str(e.value) and "view-distance" in str(e.value)


# -- console permission mapping ------------------------------------------------------

@pytest.mark.parametrize("cmd,perm", [
    ("list", Perm.CHAT),
    ("op Steve", Perm.PLAYERS_MANAGE),
    ("/whitelist add Alex", Perm.PLAYERS_MANAGE),
    ("time set day", Perm.SERVER_CONSOLE),
    ("execute as @a run op Steve", Perm.SERVER_CONSOLE),
])
def test_console_permission(cmd, perm):
    assert console_permission({"command": cmd}) == perm


def test_console_stop_refused():
    for cmd in ("stop", "execute run stop", "minecraft:stop"):
        with pytest.raises(ToolError):
            console_permission({"command": cmd})


async def test_execute_run_op_needs_players_manage(services):
    services.perms.grant(USER.id, USER.name, Perm.SERVER_CONSOLE)
    out, err = await build_registry().execute(
        ToolContext(USER, services), "console_command", {"command": "execute as @a run op Pat"})
    assert err and "players.manage" in out


# -- download cache ------------------------------------------------------------------

async def test_cache_checksum_and_reuse(tmp_path):
    src = tmp_path / "file.bin"
    src.write_bytes(b"hello minecraft")
    digest = hashlib.sha1(b"hello minecraft").hexdigest()
    cache = Cache(tmp_path / "cache")
    p = await cache.fetch(src.as_uri(), algo="sha1", digest=digest)
    assert p == tmp_path / "cache" / "sha1" / digest and p.read_bytes() == b"hello minecraft"
    src.unlink()                       # second fetch must come from cache
    assert await cache.fetch(src.as_uri(), algo="sha1", digest=digest) == p


async def test_cache_rejects_bad_checksum(tmp_path, monkeypatch):
    monkeypatch.setattr("autoadmin.server.downloads.asyncio.sleep", _instant)
    src = tmp_path / "f"
    src.write_bytes(b"x")
    with pytest.raises(DownloadError):
        await Cache(tmp_path / "c").fetch(src.as_uri(), algo="sha1", digest="0" * 40)
    assert not any((tmp_path / "c").rglob("*.part"))


async def _instant(_):
    return None


# -- rcon ------------------------------------------------------------------------------

async def _fake_rcon_server(password: str):
    async def handle(reader, writer):
        async def read():
            (n,) = struct.unpack("<i", await reader.readexactly(4))
            d = await reader.readexactly(n)
            return struct.unpack("<ii", d[:8]) + (d[8:-2].decode(),)

        def send(req_id, kind, body):
            payload = struct.pack("<ii", req_id, kind) + body.encode() + b"\0\0"
            writer.write(struct.pack("<i", len(payload)) + payload)

        req_id, _, pw = await read()
        send(req_id if pw == password else -1, 2, "")
        if pw == password:
            req_id, _, cmd = await read()
            send(req_id, 0, f"ran {cmd}")
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_rcon_roundtrip_and_bad_password():
    server = await _fake_rcon_server("pw")
    port = server.sockets[0].getsockname()[1]
    async with server:
        assert await Rcon("127.0.0.1", port, "pw").command("list") == "ran list"
        with pytest.raises(RconError):
            await Rcon("127.0.0.1", port, "nope").command("list")
    with pytest.raises(RconError):
        await Rcon("127.0.0.1", port, "pw", timeout=1).command("list")


# -- startup detection ---------------------------------------------------------------------

def _installed(services) -> None:
    services.server.dir.mkdir(parents=True, exist_ok=True)
    services.server._save(InstallState(
        minecraft_version="1.21.1", loader="fabric", loader_version="0.20.1", java_major=21,
        launch=["-jar", "fabric-server-launch.jar"], installed_at=0, installed_by="x",
        rcon_password="pw", memory_mb=6144))


class FakeUnit:
    def __init__(self, states):
        self.states = list(states)

    async def state(self):
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr("autoadmin.server.manager.asyncio.sleep", _instant)


async def test_startup_ready(services, fast):
    _installed(services)
    console = services.server.dir / "console.log"
    console.write_text("[12:00:00] [Server thread/INFO]: Done (3.210s)! For help, type \"help\"\n")
    services.server.unit = FakeUnit([UnitState("active", None, "")])
    outcome, _ = await services.server._await_startup(console, started=__import__("time").time())
    assert outcome is StartOutcome.READY


async def test_startup_eula_from_real_log(services, fast):
    _installed(services)
    console = services.server.dir / "console.log"
    console.write_text((FIXTURES / "fabric-boot-eula.log").read_text())
    services.server.unit = FakeUnit([UnitState("inactive", 0, "success")])
    outcome, msg = await services.server._await_startup(console, started=__import__("time").time())
    assert outcome is StartOutcome.EULA_REQUIRED and "aka.ms" in msg


async def test_startup_failure_creates_incident(services, fast):
    _installed(services)
    console = services.server.dir / "console.log"
    console.write_text("starting\njava.lang.IllegalStateException: mod foo needs bar\n\tat x.y(Z.java:1)\nbye\n")
    services.server.unit = FakeUnit([UnitState("failed", 1, "exit-code")])
    outcome, msg = await services.server._await_startup(console, started=__import__("time").time())
    assert outcome is StartOutcome.FAILED
    inc_id = msg.split("incident_id: ")[1].split()[0]
    inc = services.incidents.get(inc_id)
    assert "mod foo needs bar" in inc.excerpt and "server/console.log" in inc.log_files


async def test_set_memory_guard(services):
    _installed(services)
    total = services.server.host_memory_mb()
    with pytest.raises(Exception, match="too little memory"):
        services.server.set_memory(total)
    assert "4096 MB" in services.server.set_memory(4096)


# -- EULA turn guard -----------------------------------------------------------------------

async def test_eula_needs_a_later_human_message(services):
    _installed(services)
    reg, state = build_registry(), {}
    ctx = ToolContext(ADMIN, services, turn=5, conv_state=state)
    accept, request = {"action": "accept"}, {"action": "request"}
    out, err = await reg.execute(ctx, "eula", accept)
    assert err and "request" in out
    await reg.execute(ctx, "eula", request)
    out, err = await reg.execute(ctx, "eula", accept)             # same turn
    assert err and "new message" in out
    later = ToolContext(ADMIN, services, turn=6, conv_state=state)
    out, err = await reg.execute(later, "eula", accept)
    assert not err
    assert "eula=true" in (services.server.dir / "eula.txt").read_text()
    assert services.server.state().eula_accepted_by.startswith("Ada")


async def test_install_needs_worlds_manage(services):
    out, err = await build_registry().execute(
        ToolContext(USER, services), "install_server",
        {"minecraft_version": "1.21.1", "loader": "fabric"})
    assert err and "worlds.manage" in out


# -- investigator ----------------------------------------------------------------------------

async def test_investigator_reads_redacted_and_reports(services):
    _installed(services)
    sdir = services.server.dir
    (sdir / "logs").mkdir()
    (sdir / "logs" / "latest.log").write_text("ok\nCaused by: missing dependency 'cloth-config'\n")
    (sdir / "server.properties").write_text("rcon.password=hunter2\nmotd=hi\n")
    inc = services.incidents.create("start_failed", "Server exited.", "excerpt", [sdir / "logs" / "latest.log"])

    client = FakeClient(
        response(tool_use("grep", {"path": "server/logs/latest.log", "pattern": "Caused by"}, "a"),
                 tool_use("read_file", {"path": "server/server.properties", "start_line": 1,
                                        "max_lines": 10}, "b"),
                 tool_use("read_file", {"path": "permissions.json", "start_line": 1,
                                        "max_lines": 10}, "c"),
                 stop="tool_use"),
        response(tool_use("submit_report", {
            "understood": True, "cause": "Missing cloth-config.", "confidence": "high",
            "evidence": ["Caused by: missing dependency 'cloth-config'"],
            "suggested_actions": ["install cloth-config"]}, "d"), stop="tool_use"),
    )
    client.messages = client.beta.messages
    report = json.loads(await Investigator(services, client).run(inc.id, "why?", OWNER))
    assert report["understood"] and report["incident_id"] == inc.id

    results = client.requests[1]["messages"][-1]["content"]
    assert "2: Caused by" in results[0]["content"]
    assert "hunter2" not in results[1]["content"] and "<redacted>" in results[1]["content"]
    assert results[2]["is_error"] and "not readable" in results[2]["content"]
    assert services.budget.total() > 0


def test_tool_list_fits_api_limits():
    from autoadmin.tools.registry import MAX_STRICT_TOOLS
    tools = build_registry(investigator=object()).api_tools()
    assert sum(1 for t in tools if t.get("strict")) <= MAX_STRICT_TOOLS
    assert all(t["input_schema"]["additionalProperties"] is False for t in tools)


async def test_server_settings_view_vs_change_permissions(services):
    _installed(services)
    (services.server.dir / "server.properties").write_text("difficulty=easy\nrcon.password=x\n")
    reg = build_registry()
    out, err = await reg.execute(ToolContext(USER, services), "server_settings", {"changes": []})
    view = json.loads(out)
    assert not err and view["difficulty"] == "easy" and "rcon.password" not in view
    assert view["memory-mb"] == 6144
    change = {"changes": [{"key": "difficulty", "value": "hard"}]}
    out, err = await reg.execute(ToolContext(USER, services), "server_settings", change)
    assert err and "config.edit" in out
    change["changes"].append({"key": "memory-mb", "value": "4096"})
    out, err = await reg.execute(ToolContext(ADMIN, services), "server_settings", change)
    assert not err and "4096 MB" in out and "difficulty=hard" in out
