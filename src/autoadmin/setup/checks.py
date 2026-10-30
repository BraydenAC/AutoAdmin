"""Credential and connection checks for `autoadmin setup`.

Synchronous on purpose: setup is an interactive terminal program.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import anthropic

DISCORD_API = "https://discord.com/api/v10"
# Discord's required bot User-Agent format; its edge rejects generic ones.
USER_AGENT = "DiscordBot (https://github.com/BraydenAC/AutoAdmin, 0.1)"

# Application flags: the Message Content intent, for verified and unverified apps.
MESSAGE_CONTENT_INTENT = (1 << 18) | (1 << 19)
ADMINISTRATOR = 1 << 3
REQUIRED_PERMISSIONS = {
    "View Channel": 1 << 10,
    "Send Messages": 1 << 11,
    "Attach Files": 1 << 15,
    "Read Message History": 1 << 16,
}
INVITE_PERMISSIONS = sum(REQUIRED_PERMISSIONS.values())   # 101376
TEXT_CHANNEL = 0


class CheckError(Exception):
    pass


class Unauthorized(CheckError):
    """The credential was rejected."""


class Unreachable(CheckError):
    """The service couldn't be reached; the credential may still be fine."""


Opener = Callable[..., Any]


class DiscordApi:
    """The few bot-token REST calls setup needs."""

    def __init__(self, token: str, opener: Opener = urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def get(self, path: str) -> Any:
        req = urllib.request.Request(DISCORD_API + path, headers={
            "Authorization": f"Bot {self.token}", "User-Agent": USER_AGENT})
        try:
            with self.opener(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise Unauthorized("Discord rejected the bot token") from e
            raise CheckError(f"Discord returned HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise Unreachable(f"can't reach Discord: {getattr(e, 'reason', e)}") from e

    def me(self) -> dict:
        return self.get("/users/@me")

    def application(self) -> dict:
        return self.get("/applications/@me")

    def guilds(self) -> list[dict]:
        return self.get("/users/@me/guilds")

    def guild(self, guild_id: str) -> dict:
        return self.get(f"/guilds/{guild_id}")

    def channels(self, guild_id: str) -> list[dict]:
        return self.get(f"/guilds/{guild_id}/channels")

    def member(self, guild_id: str, user_id: str) -> dict:
        return self.get(f"/guilds/{guild_id}/members/{user_id}")


def invite_url(application_id: str) -> str:
    return (f"https://discord.com/oauth2/authorize?client_id={application_id}"
            f"&scope=bot&permissions={INVITE_PERMISSIONS}")


def has_message_content(application: dict) -> bool:
    return bool(int(application.get("flags") or 0) & MESSAGE_CONTENT_INTENT)


def text_channels(channels: list[dict]) -> list[dict]:
    return sorted((c for c in channels if c.get("type") == TEXT_CHANNEL),
                  key=lambda c: (c.get("position", 0), c["name"]))


def channel_permissions(guild: dict, member: dict, channel: dict) -> int:
    """Discord's permission resolution: base role permissions, then the channel's
    @everyone, role, and member overwrites, in that order."""
    user_id = member["user"]["id"]
    if guild.get("owner_id") == user_id:
        return ~0
    roles = {r["id"]: int(r["permissions"]) for r in guild.get("roles", [])}
    member_roles = set(member.get("roles", []))
    perms = roles.get(guild["id"], 0)
    for rid in member_roles:
        perms |= roles.get(rid, 0)
    if perms & ADMINISTRATOR:
        return ~0

    overwrites = {o["id"]: o for o in channel.get("permission_overwrites", [])}

    def apply(allow: int, deny: int) -> None:
        nonlocal perms
        perms = (perms & ~deny) | allow

    if everyone := overwrites.get(guild["id"]):
        apply(int(everyone["allow"]), int(everyone["deny"]))
    allow = deny = 0
    for rid in member_roles:
        if o := overwrites.get(rid):
            allow |= int(o["allow"])
            deny |= int(o["deny"])
    apply(allow, deny)
    if o := overwrites.get(user_id):
        apply(int(o["allow"]), int(o["deny"]))
    return perms


def missing_permissions(perms: int) -> list[str]:
    return [name for name, bit in REQUIRED_PERMISSIONS.items() if not perms & bit]


def check_anthropic(key: str, models: list[str]) -> list[str]:
    """Validate the key with free model lookups. Returns the models it can't use."""
    client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20)
    missing = []
    for model in models:
        try:
            client.models.retrieve(model)
        except anthropic.NotFoundError:
            missing.append(model)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise Unauthorized("Anthropic rejected the API key") from e
        except anthropic.APIConnectionError as e:
            raise Unreachable("can't reach the Anthropic API") from e
        except anthropic.APIStatusError as e:
            raise CheckError(f"Anthropic API returned HTTP {e.status_code}") from e
    return missing
