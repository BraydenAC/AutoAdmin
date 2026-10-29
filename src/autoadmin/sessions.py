"""Conversation tracking and routing, independent of the chat platform.

A conversation starts when someone @mentions the bot, and continues whenever
anyone replies to a message in it. Each message runs as its own author.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from . import noai
from .agent import Agent
from .auth import Perm, Principal
from .budget import BudgetEvent
from .services import Services

IDLE_EXPIRY_S = 24 * 3600


@dataclass
class IncomingMessage:
    id: str
    author: Principal
    text: str                       # bot mention removed; user mentions as @name
    mentions: dict[str, str]        # user id -> name, excluding the bot
    mentions_bot: bool
    reply_to: str | None = None


@dataclass
class Outgoing:
    text: str
    reply_to: str | None = None
    ping_ids: list[str] = field(default_factory=list)


# Posts a message and returns its id, or the ids of every part if it was split.
Send = Callable[[Outgoing], Awaitable[str | list[str]]]

_ids = itertools.count(1)


@dataclass
class Conversation:
    id: int = field(default_factory=lambda: next(_ids))
    messages: list = field(default_factory=list)
    message_ids: set[str] = field(default_factory=set)
    state: dict = field(default_factory=dict)       # tool scratch state
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.time)


class Router:
    def __init__(self, services: Services, agent: Agent):
        self.services = services
        self.agent = agent
        self._by_message: dict[str, Conversation] = {}
        self._turns = itertools.count(1)

    def accepts(self, msg: IncomingMessage) -> bool:
        """Whether this message is addressed to the bot (cheap, no side effects)."""
        return bool(msg.mentions_bot or (msg.reply_to and msg.reply_to in self._by_message))

    def anchor(self, message_id: str) -> None:
        """Make a standalone bot message (e.g. the greeting) start a conversation on reply."""
        self._track(Conversation(), message_id)

    async def _post(self, conv: Conversation, send: Send, out: Outgoing) -> str:
        ids = await send(out)
        ids = [ids] if isinstance(ids, str) else ids
        for i in ids:
            self._track(conv, i)
        return ids[-1]

    def _locate(self, msg: IncomingMessage) -> Conversation | None:
        if msg.reply_to and msg.reply_to in self._by_message:
            return self._by_message[msg.reply_to]
        if msg.mentions_bot:
            return Conversation()
        return None

    def _track(self, conv: Conversation, message_id: str) -> None:
        conv.message_ids.add(message_id)
        self._by_message[message_id] = conv

    def _prune(self) -> None:
        cutoff = time.time() - IDLE_EXPIRY_S
        stale = {c.id for c in self._by_message.values() if c.last_active < cutoff}
        if stale:
            self._by_message = {k: c for k, c in self._by_message.items() if c.id not in stale}

    def _header(self, msg: IncomingMessage) -> str:
        role = self.services.perms.role(msg.author)
        head = f"[from {msg.author.name} id={msg.author.id} role={role}"
        if msg.mentions:
            head += " | mentions: " + ", ".join(f"{n} id={i}" for i, n in msg.mentions.items())
        return head + "]"

    async def handle(self, msg: IncomingMessage, send: Send) -> None:
        perms = self.services.perms
        self._prune()
        conv = self._locate(msg)
        if conv is None:
            return
        perms.remember(msg.author)
        if not perms.check(msg.author, Perm.CHAT).allowed:
            return  # blocked users are told once, at block time
        self._track(conv, msg.id)

        async with conv.lock:
            conv.last_active = time.time()
            last_bot_id: str | None = None

            async def emit(text: str) -> None:
                nonlocal last_bot_id
                if last_bot_id is None:
                    out = Outgoing(text, reply_to=msg.id)
                else:
                    # Second message in a row: Discord won't notify on a reply to
                    # ourselves, so ping the person explicitly.
                    out = Outgoing(text, reply_to=last_bot_id, ping_ids=[msg.author.id])
                last_bot_id = await self._post(conv, send, out)

            if self.services.budget.exhausted():
                await emit(await noai.handle(self.services, msg.author, msg.text))
            else:
                conv.messages.append(
                    {"role": "user", "content": f"{self._header(msg)}\n{msg.text}"})
                await self.agent.run(conv.messages, msg.author, msg.mentions, emit,
                                     turn=next(self._turns), conv_state=conv.state)

            await self._announce_budget(conv, last_bot_id, send)

    async def _announce_budget(self, conv: Conversation, reply_to: str | None,
                               send: Send) -> None:
        perms, budget = self.services.perms, self.services.budget
        admins = [i for i in [perms.owner_id, *perms.admin_ids] if i]
        for event in budget.pending_events():
            s = budget.status()
            if event is BudgetEvent.WARNING:
                text = (f"Heads up: I've used ${s['spent_usd']:.2f} of my "
                        f"${s['cap_usd']:.2f} AI budget.")
            else:
                text = ("My AI budget is used up. Until it's raised I can only do "
                        "`status`, `start`, and `stop`.")
            await self._post(conv, send, Outgoing(text, reply_to=reply_to, ping_ids=admins))
