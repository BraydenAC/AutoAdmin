from __future__ import annotations

import asyncio

from autoadmin.auth import SYSTEM, Perm
from autoadmin.config import LimitsConfig
from autoadmin.limits import RateLimiter
from autoadmin.sessions import Router
from autoadmin.tools import ToolContext, build_registry

from .conftest import ADMIN, OWNER, USER
from .test_agent_router import Channel, msg


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def limiter(services, **cfg):
    clock = Clock()
    return RateLimiter(LimitsConfig(**cfg), services.perms, clock), clock


def test_message_rate_tells_once_then_recovers(services):
    lim, clock = limiter(services, messages_per_minute=3, messages_per_hour=100)
    assert [lim.admit(USER)[0] for _ in range(3)] == [True] * 3
    ok, notice = lim.admit(USER)
    assert not ok and "Try again in 60 seconds" in notice
    assert lim.admit(USER) == (False, None)                   # told once, then silence
    clock.t += 61
    assert lim.admit(USER) == (True, None)


def test_hourly_message_cap(services):
    lim, clock = limiter(services, messages_per_minute=100, messages_per_hour=5)
    for _ in range(5):
        assert lim.admit(USER)[0]
        clock.t += 61
    ok, notice = lim.admit(USER)
    assert not ok and "minutes" in notice


def test_owner_admins_and_system_are_exempt(services):
    lim, _ = limiter(services, messages_per_minute=1, heavy_actions_per_hour=1)
    for p in (OWNER, ADMIN, SYSTEM):
        assert all(lim.admit(p)[0] for _ in range(10))
        lim.record_heavy(p)
        assert lim.heavy_wait(p) == 0
    strict, _ = limiter(services, messages_per_minute=1, exempt_admins=False)
    assert strict.admit(ADMIN)[0] and not strict.admit(ADMIN)[0]


def test_requests_in_progress(services):
    lim, _ = limiter(services, requests_in_progress=1)
    assert lim.admit(USER)[0]
    lim.started(USER)
    ok, notice = lim.admit(USER)
    assert not ok and "still working" in notice
    lim.finished(USER)
    assert lim.admit(USER)[0]


async def test_router_refuses_without_running_the_agent(services):
    services.limits, _ = limiter(services, messages_per_minute=1)

    class Agent:
        runs = 0

        async def run(self, messages, author, mentions, emit, *, turn, conv_state):
            Agent.runs += 1
            await emit("ok")

    router, ch = Router(services, Agent()), Channel()
    await router.handle(msg("m1", USER, bot=True), ch.send)
    await router.handle(msg("m2", USER, bot=True), ch.send)
    await router.handle(msg("m3", USER, bot=True), ch.send)
    assert Agent.runs == 1
    assert [o.text for o in ch.sent][1].startswith("Pat, you're sending messages")
    assert len(ch.sent) == 2                                   # one notice, then silence
    assert not router.accepts(msg("m4", USER, reply_to="m2"))  # refused one isn't tracked


async def test_parallel_requests_are_capped(services):
    services.limits, _ = limiter(services, requests_in_progress=1)
    gate = asyncio.Event()

    class Agent:
        async def run(self, messages, author, mentions, emit, *, turn, conv_state):
            await gate.wait()
            await emit("done")

    router, ch = Router(services, Agent()), Channel()
    first = asyncio.create_task(router.handle(msg("m1", USER, bot=True), ch.send))
    await asyncio.sleep(0)
    await router.handle(msg("m2", USER, bot=True), ch.send)   # a second conversation
    assert "still working" in ch.sent[0].text
    gate.set()
    await first
    await router.handle(msg("m3", USER, bot=True), ch.send)
    assert [o.text for o in ch.sent][-1] == "done"


async def test_heavy_actions_are_limited_per_hour(services, monkeypatch):
    services.limits, clock = limiter(services, heavy_actions_per_hour=2)
    services.perms.grant(USER.id, USER.name, Perm.SERVER_CONTROL)

    async def fake_stop(self):
        return "Server stopped."
    monkeypatch.setattr(type(services.server), "stop", fake_stop)
    reg = build_registry()
    stop = {"action": "stop"}
    for _ in range(2):
        out, err = await reg.execute(ToolContext(USER, services), "server_power", stop)
        assert not err, out
    out, err = await reg.execute(ToolContext(USER, services), "server_power", stop)
    assert err and out.startswith("RATE LIMITED") and "60 minutes" in out
    out, err = await reg.execute(ToolContext(ADMIN, services), "server_power", stop)
    assert not err                                             # admins aren't limited
    out, err = await reg.execute(ToolContext(USER, services), "server_status", {})
    assert not err                                             # light tools aren't counted
    clock.t += 3601
    out, err = await reg.execute(ToolContext(USER, services), "server_power", stop)
    assert not err
