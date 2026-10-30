"""Tools for installing, configuring, and running the Minecraft server."""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

from ..auth import Perm
from ..server.manager import EULA_URL, ServerError, StartOutcome
from .registry import Registry, ToolContext, ToolError

if TYPE_CHECKING:
    from ..agent.investigator import Investigator

# Console commands that duplicate a finer-grained permission.
PLAYER_COMMANDS = {"op", "deop", "ban", "ban-ip", "pardon", "pardon-ip", "kick", "whitelist"}
LIFECYCLE_COMMANDS = {"stop", "restart"}
READONLY_COMMANDS = {"list", "help"}


def _verbs(command: str) -> list[str]:
    """Every command verb in a console line, including those after `execute ... run`."""
    try:
        words = shlex.split(command.removeprefix("/"))
    except ValueError:
        words = command.removeprefix("/").split()
    verbs = words[:1]
    verbs += [words[i + 1] for i, w in enumerate(words[:-1]) if w == "run"]
    return [v.lower().removeprefix("minecraft:") for v in verbs]


def console_permission(args: dict[str, Any]) -> Perm:
    verbs = _verbs(args.get("command", ""))
    if not verbs:
        raise ToolError("empty command")
    if LIFECYCLE_COMMANDS & set(verbs):
        raise ToolError("use server_power to stop or restart the server")
    if verbs == [verbs[0]] and verbs[0] in READONLY_COMMANDS:
        return Perm.CHAT
    if verbs == [verbs[0]] and verbs[0] in PLAYER_COMMANDS:
        return Perm.PLAYERS_MANAGE
    return Perm.SERVER_CONSOLE


MEMORY_KEY = "memory-mb"


def _settings_permission(args: dict[str, Any]) -> Perm:
    return Perm.CONFIG_EDIT if args.get("changes") else Perm.CHAT


def _wrap(e: ServerError) -> ToolError:
    return ToolError(str(e))


async def _start(ctx: ToolContext) -> str:
    try:
        outcome, message = await ctx.services.server.start(ctx.say)
    except ServerError as e:
        raise _wrap(e)
    if outcome in (StartOutcome.FAILED, StartOutcome.TIMEOUT):
        raise ToolError(f"{message}\nUse investigate(incident_id) for a diagnosis.")
    return message


def register(r: Registry, investigator: Investigator | None = None) -> None:

    @r.tool("server_status",
            "Installed version/loader, running state, online players, memory, EULA status.",
            permission=Perm.CHAT)
    async def server_status(ctx: ToolContext) -> str:
        return json.dumps(await ctx.services.server.status())

    @r.tool("eula",
            "Minecraft EULA acceptance in two steps. action=request: returns the link to show the "
            "requester. action=accept: records acceptance; only works after request and an "
            "explicit yes from a human in a later message. Never accept on your own.",
            permission=Perm.WORLDS_MANAGE,
            properties={"action": {"type": "string", "enum": ["request", "accept"]}})
    async def eula(ctx: ToolContext, action: str) -> str:
        if action == "request":
            ctx.conv_state["eula_prompt_turn"] = ctx.turn
            return (f"Ask the requester to read {EULA_URL} and reply yes to accept it. "
                    "Do not accept in this turn.")
        prompted = ctx.conv_state.get("eula_prompt_turn")
        if prompted is None:
            raise ToolError("call eula(action=request) first")
        if prompted >= ctx.turn:
            raise ToolError("the requester must confirm in a new message first")
        try:
            return ctx.services.server.accept_eula(str(ctx.principal))
        except ServerError as e:
            raise _wrap(e)

    @r.tool("server_settings",
            "View or change server settings. With an empty `changes` list, returns current values. "
            "Keys: server.properties entries (level-type: normal/flat/large_biomes/amplified, "
            "level-seed, gamemode, difficulty, hardcore, pvp, motd, max-players, view-distance, ...) "
            "plus memory-mb (server heap). World-generation keys only affect a world not yet "
            "generated. Changes take effect on next start.",
            permission=_settings_permission,
            properties={"changes": {
                "type": "array",
                "items": {"type": "object",
                          "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                          "required": ["key", "value"], "additionalProperties": False}}})
    async def server_settings(ctx: ToolContext, changes: list[dict]) -> str:
        server = ctx.services.server
        try:
            if not changes:
                st = server.state()
                return json.dumps({**server.get_properties(),
                                   MEMORY_KEY: st.memory_mb if st else None})
            wanted = {c["key"]: c["value"] for c in changes}
            notes = []
            if MEMORY_KEY in wanted:
                raw = wanted.pop(MEMORY_KEY)
                if not str(raw).isdigit():
                    raise ToolError(f"{MEMORY_KEY} must be a whole number of MB")
                notes.append(server.set_memory(int(raw)))
            if wanted:
                done = server.set_properties(wanted)
                notes.append("Set " + ", ".join(f"{k}={v}" for k, v in done.items()) + ".")
            return " ".join(notes)
        except ServerError as e:
            raise _wrap(e)

    @r.tool("server_power",
            "start: start and block until the server is up, stops for the EULA, fails, or times "
            "out (failures return an excerpt and incident_id). stop: stop cleanly, saving the "
            "world. restart: stop then start.",
            permission=Perm.SERVER_CONTROL,
            properties={"action": {"type": "string", "enum": ["start", "stop", "restart"]}})
    async def server_power(ctx: ToolContext, action: str) -> str:
        if action in ("stop", "restart"):
            try:
                stopped = await ctx.services.server.stop()
            except ServerError as e:
                raise _wrap(e)
            if action == "stop":
                return stopped
        return await _start(ctx)

    @r.tool("console_command",
            "Run a command on the server console (no leading slash needed). "
            "Not for stopping the server (use server_power).",
            permission=console_permission,
            properties={"command": {"type": "string"}})
    async def console_command(ctx: ToolContext, command: str) -> str:
        # `execute ... run <verb>` can hide a player-management command.
        if {v for v in _verbs(command)[1:]} & PLAYER_COMMANDS:
            ctx.check(Perm.PLAYERS_MANAGE)
        try:
            return await ctx.services.server.console(command)
        except ServerError as e:
            raise _wrap(e)

    if investigator is not None:
        @r.tool("investigate",
                "Hand an incident to a read-only investigator sub-agent. Returns a short report "
                "(understood, cause, confidence, evidence, suggested_actions). You decide what to do.",
                permission=Perm.SERVER_CONTROL,
                properties={"incident_id": {"type": "string"},
                            "question": {"type": "string",
                                         "description": "What you want to know, in one sentence."}})
        async def investigate(ctx: ToolContext, incident_id: str, question: str) -> str:
            await ctx.say("Looking into it…")
            return await investigator.run(incident_id, question, ctx.principal)
