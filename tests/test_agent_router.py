from __future__ import annotations

import json

from autoadmin.agent import Agent
from autoadmin.auth import Perm
from autoadmin.sessions import IncomingMessage, Outgoing, Router
from autoadmin.tools import ToolContext, build_registry

from .conftest import ADMIN, OTHER, OWNER, USER, FakeClient, response, text, tool_use


def _audit(services) -> list[dict]:
    return [json.loads(l) for l in services.audit.path.read_text().splitlines()]


class Channel:
    def __init__(self):
        self.sent: list[Outgoing] = []

    async def send(self, out: Outgoing) -> str:
        self.sent.append(out)
        return f"bot-{len(self.sent)}"


def _router(services, *responses):
    client = FakeClient(*responses)
    return Router(services, Agent(services, build_registry(), client=client)), client


def msg(id, author, text_="hi", *, bot=False, reply_to=None, mentions=None):
    return IncomingMessage(id=id, author=author, text=text_, mentions=mentions or {},
                           mentions_bot=bot, reply_to=reply_to)


# -- registry gate -----------------------------------------------------------

async def test_registry_denies_and_audits(services):
    reg = build_registry()
    out, err = await reg.execute(ToolContext(USER, services), "set_user_access",
                                {"user_id": OTHER.id, "action": "grant_admin", "permission": "none"})
    assert err and out.startswith("DENIED")
    entry = _audit(services)[-1]
    assert entry["allowed"] is False and entry["principal"].startswith("Pat")


async def test_grant_admin_requires_real_mention(services):
    reg = build_registry()
    args = {"user_id": OTHER.id, "action": "grant_admin", "permission": "none"}
    services.perms.remember(OTHER)            # known, but not mentioned now
    out, err = await reg.execute(ToolContext(OWNER, services), "set_user_access", args)
    assert err and "@mentioned" in out
    ctx = ToolContext(OWNER, services, mentions={OTHER.id: OTHER.name})
    out, err = await reg.execute(ctx, "set_user_access", args)
    assert not err and services.perms.role(OTHER) == "admin"


# -- agent loop ----------------------------------------------------------------

async def test_tool_runs_as_message_author(services):
    router, client = _router(
        services,
        response(tool_use("set_user_access", {"user_id": OTHER.id, "action": "grant_admin",
                                              "permission": "none"}), stop="tool_use"),
        response(text("You can't do that.")),
    )
    ch = Channel()
    await router.handle(msg("1", USER, "make sam admin", bot=True,
                            mentions={OTHER.id: OTHER.name}), ch.send)
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] and "DENIED for Pat" in tool_result["content"]
    assert services.perms.role(OTHER) == "user"
    assert [o.text for o in ch.sent] == ["You can't do that."]


async def test_header_identifies_each_author(services):
    router, client = _router(services, response(text("hey")), response(text("hi Sam")))
    ch = Channel()
    await router.handle(msg("1", USER, "hello", bot=True), ch.send)
    await router.handle(msg("2", OTHER, "me too", reply_to="bot-1"), ch.send)
    second = client.requests[1]["messages"]
    assert second[0]["content"].startswith("[from Pat id=u-user role=user]")
    assert second[-1]["content"].startswith("[from Sam id=u-other role=user]")


async def test_second_message_in_a_row_pings_author(services):
    router, _ = _router(
        services,
        response(text("On it."), tool_use("access_overview", {"view": "me"}), stop="tool_use"),
        response(text("Done.")),
    )
    ch = Channel()
    await router.handle(msg("1", USER, "who am i", bot=True), ch.send)
    first, second = ch.sent
    assert first.reply_to == "1" and first.ping_ids == []
    assert second.reply_to == "bot-1" and second.ping_ids == [USER.id]


async def test_unrelated_messages_ignored(services):
    router, client = _router(services)
    ch = Channel()
    await router.handle(msg("1", USER, "just chatting"), ch.send)
    await router.handle(msg("2", USER, "reply to a human", reply_to="999"), ch.send)
    assert ch.sent == [] and client.requests == []


async def test_user_blocked_at_spend_limit(services):
    # 600k tokens at $1/M = $0.60, over 5% of $10.
    router, client = _router(
        services,
        response(tool_use("access_overview", {"view": "me"}), stop="tool_use", tokens=600_000),
    )
    ch = Channel()
    await router.handle(msg("1", USER, "hi", bot=True), ch.send)
    assert services.perms.is_blocked(USER.id)
    assert "paused your access" in ch.sent[-1].text
    await router.handle(msg("2", USER, "hello?", reply_to="bot-1"), ch.send)
    assert len(client.requests) == 1 and len(ch.sent) == 1   # silently ignored


async def test_admin_never_blocked(services):
    router, _ = _router(services, response(text("ok"), tokens=2_000_000))
    await router.handle(msg("1", ADMIN, "hi", bot=True), Channel().send)
    assert not services.perms.is_blocked(ADMIN.id)


async def test_warning_and_no_ai_mode(services):
    router, client = _router(services, response(text("sure"), tokens=10_000_000))
    ch = Channel()
    await router.handle(msg("1", OWNER, "hi", bot=True), ch.send)
    # One AI reply, then the exhausted announcement pinging owner + admins.
    assert ch.sent[0].text == "sure"
    assert "used up" in ch.sent[1].text
    assert set(ch.sent[1].ping_ids) == {OWNER.id, ADMIN.id}

    await router.handle(msg("2", USER, "status", bot=True), ch.send)
    assert ch.sent[-1].text == "No server is set up yet."
    await router.handle(msg("3", USER, "start", bot=True), ch.send)
    assert "missing permission 'server.control'" in ch.sent[-1].text
    await router.handle(msg("4", USER, "tell me a joke", bot=True), ch.send)
    assert "only do" in ch.sent[-1].text
    assert len(client.requests) == 1


async def test_set_budget_cap_needs_permission(services):
    reg = build_registry()
    out, err = await reg.execute(ToolContext(ADMIN, services), "set_budget_cap", {"usd": 50})
    assert err
    services.perms.set_budget_manager(ADMIN.id, True)
    out, err = await reg.execute(ToolContext(ADMIN, services), "set_budget_cap", {"usd": 50})
    assert not err and services.budget.cap == 50


async def test_grant_permission_flow(services):
    reg = build_registry()
    ctx = ToolContext(ADMIN, services, mentions={USER.id: USER.name})
    out, err = await reg.execute(ctx, "set_user_access",
                                 {"user_id": USER.id, "action": "grant_permission",
                                  "permission": "server.control"})
    assert not err
    out, err = await reg.execute(ctx, "set_user_access",
                                 {"user_id": USER.id, "action": "grant_permission",
                                  "permission": "none"})
    assert err and "needs a permission" in out
    assert services.perms.check(USER, Perm.SERVER_CONTROL).allowed


async def test_admin_cannot_manage_admins_via_merged_tool(services):
    ctx = ToolContext(ADMIN, services, mentions={USER.id: USER.name})
    out, err = await build_registry().execute(
        ctx, "set_user_access", {"user_id": USER.id, "action": "grant_admin", "permission": "none"})
    assert err and "admins.manage" in out


async def test_overview_views(services):
    reg = build_registry()
    me = json.loads((await reg.execute(ToolContext(USER, services), "access_overview",
                                       {"view": "me"}))[0])
    assert me["permissions"] == ["chat"]
    res = json.loads((await reg.execute(ToolContext(USER, services), "access_overview",
                                        {"view": "resources"}))[0])
    assert res["host"]["cpu_cores"] >= 1 and res["server_process"] == {"running": False}
    assert set(res["runtime_mb"]) >= {"world_saves", "backups", "java_runtimes", "total"}
