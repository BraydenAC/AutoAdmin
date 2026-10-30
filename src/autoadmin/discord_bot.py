"""Discord adapter: one guild, one channel, everything else is the router's job."""

from __future__ import annotations

import logging
import re
from typing import Any

import discord

from .agent import Agent
from .agent.investigator import Investigator
from .auth import Principal
from .config import Config
from .services import Services
from .sessions import Attachment, IncomingMessage, Outgoing, Router
from .tools import build_registry

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
USER_MENTION = re.compile(r"<@!?(\d+)>")
ROLE_MENTION = re.compile(r"<@&(\d+)>")


def to_incoming(message: Any, bot_id: int) -> IncomingMessage:
    """Convert a Discord message. Only mentions typed in the text count as mentions;
    Discord also lists the replied-to user, which must not grant them anything."""
    content: str = message.content
    typed = {int(m) for m in USER_MENTION.findall(content)}
    names = {u.id: u.display_name for u in message.mentions}
    mentions = {str(uid): names.get(uid, str(uid)) for uid in typed if uid != bot_id}

    def render(m: re.Match) -> str:
        uid = int(m.group(1))
        return "" if uid == bot_id else f"@{names.get(uid, uid)}"

    text = USER_MENTION.sub(render, content)
    text = ROLE_MENTION.sub("@role", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    ref = message.reference
    reply_to = str(ref.message_id) if ref is not None and ref.message_id else None
    author = Principal(str(message.author.id), message.author.display_name)
    attachments = [Attachment(a.filename, a.size, a.url) for a in message.attachments]
    return IncomingMessage(
        id=str(message.id), author=author, text=text, mentions=mentions,
        mentions_bot=bot_id in typed, reply_to=reply_to, attachments=attachments)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line boundaries where possible."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text or not chunks:
        chunks.append(text)
    return chunks


class Sender:
    """Posts Outgoing messages. Pings only the users the harness chose."""

    def __init__(self, channel: Any):
        self.channel = channel

    async def __call__(self, out: Outgoing) -> list[str]:
        prefix = "".join(f"<@{uid}> " for uid in out.ping_ids)
        allowed = discord.AllowedMentions(
            everyone=False, roles=False,
            users=[discord.Object(int(uid)) for uid in out.ping_ids],
            replied_user=True)
        ids: list[str] = []
        reply_to = out.reply_to
        for chunk in split_message(prefix + out.text):
            ref = (discord.MessageReference(message_id=int(reply_to), channel_id=self.channel.id,
                                            fail_if_not_exists=False) if reply_to else None)
            sent = await self.channel.send(chunk, reference=ref, allowed_mentions=allowed)
            ids.append(str(sent.id))
            reply_to = ids[-1]
        return ids


def greeting(services: Services) -> str:
    st = services.server.state()
    if st is None:
        return ("AutoAdmin is ready to set up a Minecraft server. @mention me with what you want, "
                "for example: \"@AutoAdmin set up a 1.21.1 Fabric superflat server\".")
    loader = st.loader if not st.loader_version else f"{st.loader} {st.loader_version}"
    return f"AutoAdmin is back online. Minecraft {st.minecraft_version} ({loader}) is installed."


class AutoAdminBot(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.cfg = config.discord
        self.services = Services.from_config(config)
        self.router = Router(self.services, Agent(
            self.services, build_registry(Investigator(self.services))))
        self.channel: discord.TextChannel | None = None
        self.send: Sender | None = None
        self._greeted = False

    async def on_ready(self) -> None:
        guild = self.get_guild(self.cfg.guild_id)
        if guild is None:
            log.error("bot is not in guild %s; check guild_id and the invite", self.cfg.guild_id)
            await self.close()
            return
        channel = guild.get_channel(self.cfg.channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("channel %s not found in %s", self.cfg.channel_id, guild.name)
            await self.close()
            return
        self.channel, self.send = channel, Sender(channel)

        owner = await guild.fetch_member(guild.owner_id)
        self.services.perms.set_owner(str(owner.id), owner.display_name)
        log.info("connected as %s to #%s in %s (owner %s)", self.user, channel.name,
                 guild.name, owner.display_name)

        if not self._greeted:   # on_ready fires again after reconnects
            self._greeted = True
            ids = await self.send(Outgoing(greeting(self.services)))
            self.router.anchor(ids[-1])

    async def on_message(self, message: discord.Message) -> None:
        if (self.send is None or message.author.bot or message.guild is None
                or message.channel.id != self.cfg.channel_id):
            return
        incoming = to_incoming(message, self.user.id)
        if not self.router.accepts(incoming):
            return
        async with message.channel.typing():
            await self.router.handle(incoming, self.send)


def run(config: Config, token: str) -> None:
    if not config.discord.guild_id or not config.discord.channel_id:
        raise SystemExit("Set [discord] guild_id and channel_id in config.toml")
    AutoAdminBot(config).run(token, log_handler=None)
