"""A terminal stand-in for Discord, for testing the agent without a bot.

    /as <name>            speak as another user (created on first use)
    /reply <n> <text>     reply to message #n (continues its conversation)
    /perms  /budget       inspect state without calling the AI
    /quit
    anything else         a new message; include @bot to start a conversation

Mentions are written @name. Each name maps to a stable fake ID `cli-<name>`.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re

from .agent import Agent
from .agent.investigator import Investigator
from .auth import Principal
from .config import Config
from .services import Services
from .sessions import IncomingMessage, Outgoing, Router
from .supervisor import Supervisor
from .tools import build_registry

BOT_NAMES = {"bot", "autoadmin"}
MENTION = re.compile(r"@(\w+)")


def _user(name: str) -> Principal:
    return Principal(id=f"cli-{name.lower()}", name=name)


def _parse(text: str) -> tuple[str, dict[str, str], bool]:
    mentions, mentions_bot = {}, False
    for name in MENTION.findall(text):
        if name.lower() in BOT_NAMES:
            mentions_bot = True
        else:
            u = _user(name)
            mentions[u.id] = u.name
    cleaned = re.sub(r"@(?:%s)\b" % "|".join(BOT_NAMES), "", text, flags=re.I).strip()
    return cleaned, mentions, mentions_bot


async def run(config: Config) -> None:
    services = Services.from_config(config)
    owner = _user(config.cli_owner)
    services.perms.set_owner(owner.id, owner.name)
    investigator = Investigator(services)
    router = Router(services, Agent(services, build_registry(investigator)))
    counter = itertools.count(1)
    me = owner

    async def send(out: Outgoing) -> str:
        n = str(next(counter))
        meta = f"↪#{out.reply_to}" if out.reply_to else ""
        if out.ping_ids:
            meta += " " + " ".join("@" + services.perms.name_of(i) for i in out.ping_ids)
        print(f"  #{n} AutoAdmin {meta}: {out.text}")
        return n

    supervisor = asyncio.create_task(Supervisor(services, router, send, investigator).run())
    print(__doc__)
    print(f"You are {me.name} (owner). Model: {config.agent.model}. "
          f"Every AI reply spends real budget.\n")
    while True:
        try:
            line = (await asyncio.to_thread(input, f"{me.name}> ")).strip()
        except (EOFError, KeyboardInterrupt):
            return
        if not line:
            continue
        reply_to = None
        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            match cmd:
                case "/quit":
                    return
                case "/as":
                    me = _user(rest.strip() or config.cli_owner)
                    print(f"  now speaking as {me.name} ({services.perms.role(me)})")
                    continue
                case "/perms":
                    print(json.dumps(services.perms.summary(), indent=2))
                    continue
                case "/budget":
                    print(json.dumps(services.budget.status(), indent=2))
                    continue
                case "/reply":
                    num, _, line = rest.partition(" ")
                    reply_to = num.lstrip("#")
                case _:
                    print("  unknown command")
                    continue
        text, mentions, mentions_bot = _parse(line)
        n = str(next(counter))
        print(f"  #{n} {me.name}" + (f" ↪#{reply_to}" if reply_to else ""))
        await router.handle(IncomingMessage(
            id=n, author=me, text=text, mentions=mentions,
            mentions_bot=mentions_bot, reply_to=reply_to), send)
