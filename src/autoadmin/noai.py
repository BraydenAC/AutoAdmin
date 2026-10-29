"""Deterministic fallback used once the AI budget is exhausted."""

from __future__ import annotations

from .auth import Perm, Principal
from .server.manager import ServerError
from .services import Services

COMMANDS = {"status": Perm.CHAT, "start": Perm.SERVER_CONTROL, "stop": Perm.SERVER_CONTROL}
HELP = "AI budget is used up. I can only do `status`, `start`, or `stop` until an admin raises it."


async def _run(services: Services, cmd: str) -> str:
    server = services.server
    if server.state() is None:
        return "No server is set up yet."
    try:
        if cmd == "status":
            s = await server.status()
            text = f"{s['minecraft_version']} ({s['loader']}) is {s['state']}."
            return text + (f" {s['players']}" if "players" in s else "")
        if cmd == "start":
            _, message = await server.start()
            return message
        return await server.stop()
    except ServerError as e:
        return str(e)


async def handle(services: Services, principal: Principal, text: str) -> str:
    words = text.strip().lower().split()
    cmd = words[0].strip("!/.,") if words else ""
    if cmd not in COMMANDS:
        return HELP
    decision = services.perms.check(principal, COMMANDS[cmd])
    result = f"Sorry, {decision.reason}." if not decision.allowed else await _run(services, cmd)
    services.audit.record(principal=str(principal), tool=f"noai.{cmd}", args={},
                          allowed=decision.allowed, result=result)
    return result
