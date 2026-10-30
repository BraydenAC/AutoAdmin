from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

from autoadmin.discord_bot import Sender, greeting, split_message, to_incoming
from autoadmin.service import render_bot_unit
from autoadmin.sessions import IncomingMessage, Outgoing, Router

from .conftest import USER

BOT = 999


def user(id, name):
    return NS(id=id, display_name=name)


def message(content, *, author=user(1, "Pat"), mentions=(), reply_to=None, id=50, attachments=()):
    ref = NS(message_id=reply_to) if reply_to else None
    return NS(id=id, content=content, author=author, mentions=list(mentions),
              reference=ref, attachments=list(attachments))


def test_mention_conversion():
    m = message(f"<@{BOT}> make <@!2> and <@3> admins, ping <@&7>",
                mentions=[user(BOT, "AutoAdmin"), user(2, "Sam"), user(3, "Ada")])
    inc = to_incoming(m, BOT)
    assert inc.mentions_bot
    assert inc.mentions == {"2": "Sam", "3": "Ada"}
    assert inc.text == "make @Sam and @Ada admins, ping @role"
    assert inc.author.id == "1" and inc.author.name == "Pat"


def test_reply_ping_is_not_a_mention():
    # Discord lists the replied-to user in mentions even when not typed.
    m = message("yes please", mentions=[user(BOT, "AutoAdmin"), user(2, "Sam")], reply_to=77)
    inc = to_incoming(m, BOT)
    assert not inc.mentions_bot and inc.mentions == {} and inc.reply_to == "77"


def test_attachments_metadata_only():
    m = message("here", attachments=[NS(filename="mod.jar", size=1234, url="https://cdn/x")])
    atts = to_incoming(m, BOT).attachments
    assert atts[0].filename == "mod.jar" and atts[0].size == 1234


def test_split_message():
    assert split_message("short") == ["short"]
    text = "\n".join(f"line {i:04d}" for i in range(400))
    parts = split_message(text, limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n".join(parts) == text
    assert split_message("x" * 1200, limit=500) == ["x" * 500, "x" * 500, "x" * 200]


class FakeChannel:
    id = 5

    def __init__(self):
        self.sent = []

    async def send(self, content, *, reference=None, allowed_mentions=None):
        self.sent.append(NS(content=content, reference=reference, allowed=allowed_mentions))
        return NS(id=100 + len(self.sent))


async def test_sender_only_pings_chosen_users():
    ch = FakeChannel()
    ids = await Sender(ch)(Outgoing("hi @everyone <@42>", reply_to="7", ping_ids=["1"]))
    sent = ch.sent[0]
    assert ids == ["101"] and sent.content.startswith("<@1> ")
    assert sent.allowed.everyone is False and sent.allowed.roles is False
    assert [u.id for u in sent.allowed.users] == [1]           # not 42
    assert sent.reference.message_id == 7


async def test_split_reply_chain_and_router_tracks_every_part(services):
    ch = FakeChannel()
    router = Router(services, agent=None)
    router.anchor("500")
    long = "a\n" * 1500
    from autoadmin.sessions import Conversation
    conv = Conversation()
    last = await router._post(conv, Sender(ch), Outgoing(long, reply_to="9"))
    assert len(ch.sent) == 2 and last == "102"
    assert ch.sent[1].reference.message_id == 101              # parts chain together
    for mid in ("101", "102", "500"):
        assert router.accepts(IncomingMessage("x", USER, "hi", {}, False, reply_to=mid))
    assert not router.accepts(IncomingMessage("x", USER, "hi", {}, False, reply_to="404"))


def test_greeting_depends_on_install(services):
    assert "ready to set up" in greeting(services)


def test_bot_unit_render():
    unit = render_bot_unit(Path("/p/config.toml"), Path("/p/.venv/bin/autoadmin"))
    assert "ExecStart=/p/.venv/bin/autoadmin discord --config /p/config.toml" in unit
    assert "Restart=on-failure" in unit and "WorkingDirectory=/p" in unit
