from __future__ import annotations

import dataclasses
import json

import pytest

from autoadmin.auth import SYSTEM
from autoadmin.server.manager import InstallState, StartOutcome
from autoadmin.server.backend import ProcessState
from autoadmin.supervisor import Supervisor
from autoadmin.tools import ToolContext, build_registry

from .conftest import ADMIN, OWNER

RUNNING = ProcessState("active", None, "")
CRASHED = ProcessState("failed", 1, "exit-code")
CLEAN_EXIT = ProcessState("inactive", 0, "success")


class FakeUnit:
    def __init__(self, state=RUNNING, uptime=10):
        self._state, self.uptime = state, uptime

    async def state(self):
        return self._state

    async def accounting(self):
        return {"running": self._state.running, "uptime_s": self.uptime}


class FakeRouter:
    def __init__(self):
        self.posts, self.turns = [], []

    async def post_system(self, send, text, *, ping_ids=None, context=None):
        self.posts.append({"text": text, "pings": ping_ids or [], "context": context})

    async def system_turn(self, send, context):
        self.turns.append(context)


class FakeInvestigator:
    def __init__(self, *reports):
        self.reports = list(reports)
        self.calls = 0

    async def run(self, incident_id, question, principal):
        assert principal is SYSTEM
        self.calls += 1
        r = self.reports.pop(0)
        return json.dumps({"incident_id": incident_id, "confidence": "high", "evidence": [],
                           "suggested_actions": [], **r})


def understood(key):
    return {"understood": True, "cause": f"cause {key}", "cause_key": key}


UNKNOWN = {"understood": False, "cause": "no idea", "cause_key": "unknown"}


@pytest.fixture
def world(services):
    d = services.server.worlds_root / "world"
    d.mkdir(parents=True)
    (d / "console.log").write_text("java.lang.RuntimeException: boom\n\tat a.b(C.java:1)\n")
    services.jail.path("active_world").write_text("world")
    services.server.save_state(InstallState(
        minecraft_version="1.21.1", loader="fabric", loader_version="0.1", java_major=21,
        launch=["-jar", "x.jar"], installed_at=0, installed_by="t",
        memory_mb=2048), "world")
    services.server.set_desired("running")
    services.server.backend = FakeUnit(CRASHED)
    return d


def make(services, *reports, starts=None, **cfg):
    if cfg:
        services.config = dataclasses.replace(
            services.config, supervisor=dataclasses.replace(services.config.supervisor, **cfg))
    router, inv = FakeRouter(), FakeInvestigator(*reports)
    sup = Supervisor(services, router, send=None, investigator=inv)
    outcomes = list(starts or [])
    started = []

    async def fake_start(progress=None):
        started.append(1)
        outcome, msg = outcomes.pop(0) if outcomes else (StartOutcome.READY, "Server is up (5s).")
        services.server.backend._state = RUNNING if outcome is StartOutcome.READY else CRASHED
        if outcome is StartOutcome.READY:
            services.server.set_desired("running")
        return outcome, msg
    services.server.start = fake_start
    return sup, router, inv, started


def crash(services):
    services.server.backend._state = CRASHED


# -- policy ----------------------------------------------------------------------------------

async def test_understood_crash_gets_fix_turn_then_restart(services, world):
    sup, router, inv, started = make(services, understood("mod-crash:foo"))
    await sup.tick()
    assert inv.calls == 1 and len(router.turns) == 1 and started == [1]
    assert "reversible fix" in router.turns[0] and "boom" in router.turns[0]
    assert router.posts[-1]["text"].startswith("Restarted.")
    assert not sup.state.escalated and len(sup.state.chain) == 1


async def test_unknown_then_crash_again_escalates(services, world):
    sup, router, _, started = make(services, UNKNOWN, understood("oom"))
    await sup.tick()
    assert router.turns == [] and "couldn't tell why" in router.posts[0]["text"]
    assert started == [1]
    crash(services)
    await sup.tick()
    assert started == [1]                                  # no second restart
    esc = router.posts[-1]
    assert sup.state.escalated and "stopped restarting" in esc["text"]
    assert "couldn't work out the previous crash" in esc["text"]
    assert set(esc["pings"]) == {OWNER.id, ADMIN.id}
    assert "Investigator report" in esc["context"]          # replies get the crash details
    crash(services)
    await sup.tick()                                        # stays quiet while escalated
    assert started == [1] and len(router.posts) == 3


async def test_understood_then_different_cause_keeps_restarting(services, world):
    sup, router, _, started = make(services, understood("oom"), understood("mod-crash:x"))
    await sup.tick()
    crash(services)
    await sup.tick()
    assert started == [1, 1] and not sup.state.escalated


async def test_same_cause_twice_escalates(services, world):
    sup, router, _, started = make(services, understood("oom"), understood("oom"))
    await sup.tick()
    crash(services)
    await sup.tick()
    assert started == [1] and sup.state.escalated
    assert "same problem came back" in router.posts[-1]["text"]


async def test_too_many_crashes_escalates(services, world):
    keys = [understood(f"k{i}") for i in range(4)]
    sup, router, _, started = make(services, *keys, max_crashes=2)
    for _ in range(3):
        crash(services)
        await sup.tick()
    assert started == [1, 1] and sup.state.escalated
    assert "crashed 3 times" in router.posts[-1]["text"]


async def test_failed_restart_is_next_crash(services, world):
    inc = services.incidents.create("start_failed", "Server exited during startup.", "x", [])
    sup, router, inv, started = make(
        services, UNKNOWN, UNKNOWN,
        starts=[(StartOutcome.FAILED, f"failed\nincident_id: {inc.id}\n```x```")])
    await sup.tick()
    assert started == [1] and inv.calls == 2 and sup.state.escalated
    assert [e.incident_id for e in sup.state.chain][-1] == inc.id


async def test_clean_exit_means_stopped_in_game(services, world):
    sup, router, inv, started = make(services)
    services.server.backend._state = CLEAN_EXIT
    await sup.tick()
    assert services.server.desired == "stopped" and started == [] and inv.calls == 0
    assert "stopped from inside the game" in router.posts[0]["text"]


async def test_no_ai_mode_restarts_once_then_escalates(services, world):
    services.budget.set_cap(0)
    sup, router, inv, started = make(services)
    await sup.tick()
    crash(services)
    await sup.tick()
    assert inv.calls == 0 and router.turns == [] and started == [1] and sup.state.escalated


async def test_desired_stopped_is_not_a_crash(services, world):
    services.server.set_desired("stopped")
    sup, router, inv, started = make(services)
    await sup.tick()
    assert started == [] and router.posts == []


async def test_stable_uptime_and_human_start_reset(services, world):
    sup, router, _, _ = make(services, understood("oom"), stable_uptime_s=100)
    await sup.tick()
    assert len(sup.state.chain) == 1
    services.server.backend.uptime = 150
    await sup.tick()
    assert sup.state.chain == []
    sup.state.escalated = True
    await sup.tick()                                        # running again -> human fixed it
    assert not sup.state.escalated


async def test_state_survives_restart_of_bot(services, world):
    sup, _, _, _ = make(services, UNKNOWN)
    await sup.tick()
    again = Supervisor(services, FakeRouter(), None, None)
    assert len(again.state.chain) == 1 and not again.state.chain[0].understood


# -- autostart ---------------------------------------------------------------------------------

async def test_autostart_only_when_it_should_be_running(services, world):
    services.server.backend._state = ProcessState("inactive", None, "")
    sup, router, _, started = make(services)
    await sup.autostart()
    assert started == [1] and "back up" in router.posts[0]["text"]
    services.server.backend._state = ProcessState("inactive", None, "")
    services.server.set_desired("stopped")
    await sup.autostart()
    assert started == [1]


# -- crash incident ------------------------------------------------------------------------------

def test_oom_kill_is_named_in_incident(services, world):
    inc = services.server.crash_incident(ProcessState("failed", None, "oom-kill"))
    assert "memory cap" in inc.summary and "boom" in inc.excerpt


# -- system principal's limited fix powers --------------------------------------------------------

async def test_system_can_disable_mods_and_change_settings_only(services, world):
    reg = build_registry()
    ctx = ToolContext(SYSTEM, services)
    out, err = await reg.execute(ctx, "mods", {"action": "disable", "query": "", "names": ["x"], "url": ""})
    assert not err or "DENIED" not in out
    out, err = await reg.execute(ctx, "mods", {"action": "install", "query": "", "names": ["x"], "url": ""})
    assert err and "DENIED" in out
    out, err = await reg.execute(ctx, "server_settings",
                                 {"changes": [{"key": "memory-mb", "value": "3072"}]})
    assert not err and services.server.state().memory_mb == 3072
    out, err = await reg.execute(ctx, "worlds", {"action": "delete", "name": "world",
                                                 "minecraft_version": "", "loader": "",
                                                 "backup_id": "", "count": 0})
    assert err and "DENIED" in out
