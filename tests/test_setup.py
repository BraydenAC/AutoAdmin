from __future__ import annotations

import stat
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from autoadmin import service
from autoadmin.setup import checks, host, wizard
from autoadmin.setup.checks import Unauthorized
from autoadmin.setup.files import EnvFile, set_discord_ids

REPO = Path(__file__).resolve().parents[1]
G = "100"          # guild id; also the @everyone role id
BOT = "900"
VIEW, SEND, ATTACH, HISTORY = 1 << 10, 1 << 11, 1 << 15, 1 << 16
ALL_NEEDED = VIEW | SEND | ATTACH | HISTORY


# -- permission math ---------------------------------------------------------------------

def guild(everyone=ALL_NEEDED, roles=(), owner="1"):
    return {"id": G, "owner_id": owner,
            "roles": [{"id": G, "permissions": str(everyone)},
                      *({"id": rid, "permissions": str(p)} for rid, p in roles)]}


def member(*roles):
    return {"user": {"id": BOT}, "roles": list(roles)}


def overwrite(id, allow=0, deny=0):
    return {"id": id, "allow": str(allow), "deny": str(deny)}


def test_invite_permissions_match_readme():
    assert checks.INVITE_PERMISSIONS == 101376


def test_base_permissions_from_everyone_and_roles():
    perms = checks.channel_permissions(guild(everyone=VIEW, roles=[("r1", SEND | ATTACH | HISTORY)]),
                                       member("r1"), {"permission_overwrites": []})
    assert checks.missing_permissions(perms) == []


def test_channel_overwrites_apply_in_order():
    g = guild(roles=[("r1", 0)])
    # @everyone denied view in the channel, bot's role allowed it back.
    ch = {"permission_overwrites": [overwrite(G, deny=VIEW), overwrite("r1", allow=VIEW)]}
    assert checks.missing_permissions(checks.channel_permissions(g, member("r1"), ch)) == []
    # A member-specific deny wins over everything.
    ch["permission_overwrites"].append(overwrite(BOT, deny=SEND))
    assert checks.missing_permissions(checks.channel_permissions(g, member("r1"), ch)) == ["Send Messages"]
    # Without the role, the @everyone deny stands.
    assert "View Channel" in checks.missing_permissions(checks.channel_permissions(g, member(), ch))


def test_administrator_bypasses_overwrites():
    g = guild(everyone=0, roles=[("admin", checks.ADMINISTRATOR)])
    ch = {"permission_overwrites": [overwrite(BOT, deny=ALL_NEEDED)]}
    assert checks.missing_permissions(checks.channel_permissions(g, member("admin"), ch)) == []


def test_message_content_intent_flags():
    assert not checks.has_message_content({"flags": 0})
    assert checks.has_message_content({"flags": 1 << 19})   # unverified apps
    assert checks.has_message_content({"flags": 1 << 18})   # verified apps


def test_text_channels_sorted():
    chans = [{"id": "3", "name": "b", "type": 0, "position": 2}, {"id": "4", "name": "voice", "type": 2},
             {"id": "5", "name": "a", "type": 0, "position": 1}]
    assert [c["name"] for c in checks.text_channels(chans)] == ["a", "b"]


def test_discord_401_is_unauthorized():
    import urllib.error

    def opener(req, timeout):
        assert req.get_header("Authorization") == "Bot tok"
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with pytest.raises(Unauthorized):
        checks.DiscordApi("tok", opener).me()


# -- files -----------------------------------------------------------------------------

def test_env_file_preserves_other_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# comment\nexport ANTHROPIC_API_KEY=old\nOTHER=1\n")
    env = EnvFile(p)
    assert env.get("ANTHROPIC_API_KEY") == "old"
    env.set("ANTHROPIC_API_KEY", "new")
    env.set("AUTOADMIN_DISCORD_TOKEN", "tok")
    env.save()
    assert p.read_text() == "# comment\nANTHROPIC_API_KEY=new\nOTHER=1\nAUTOADMIN_DISCORD_TOKEN=tok\n"
    if sys.platform != "win32":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_set_discord_ids_in_example_config(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text((REPO / "config.example.toml").read_text())
    set_discord_ids(p, 123, 456)
    raw = tomllib.loads(p.read_text())
    assert raw["discord"] == {"guild_id": 123, "channel_id": 456,
                              "token_env": "AUTOADMIN_DISCORD_TOKEN"}
    assert raw["cli"]["owner"] == "owner"    # later sections untouched


def test_set_discord_ids_adds_missing_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[agent]\nmodel = \"x\"\n")
    set_discord_ids(p, 1, 2)
    assert tomllib.loads(p.read_text())["discord"] == {"guild_id": 1, "channel_id": 2}


# -- Windows task -------------------------------------------------------------------------

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def task(always_on: bool) -> ET.Element:
    xml = service.render_bot_task(user="PC\\Pat & Co", command=Path(r"C:\aa\.venv\Scripts\pythonw.exe"),
                                  arguments=["-m", "autoadmin", "discord", "--config", r"C:\aa dir\config.toml"],
                                  workdir=Path(r"C:\aa"), always_on=always_on)
    return ET.fromstring(xml.split("\n", 1)[1])   # drop the UTF-16 declaration


def test_task_logged_in_mode():
    t = task(always_on=False)
    assert t.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "PC\\Pat & Co"
    assert t.find("t:Principals/t:Principal/t:LogonType", NS).text == "InteractiveToken"
    # The default 72-hour limit would kill the bot.
    assert t.find("t:Settings/t:ExecutionTimeLimit", NS).text == "PT0S"
    assert t.find("t:Actions/t:Exec/t:Arguments", NS).text.endswith('--config "C:\\aa dir\\config.toml"')


def test_task_always_on_mode():
    t = task(always_on=True)
    assert t.find("t:Triggers/t:BootTrigger", NS) is not None
    assert t.find("t:Principals/t:Principal/t:LogonType", NS).text == "S4U"


def test_elevated_command_quoting(monkeypatch):
    seen = {}
    monkeypatch.setattr(host.subprocess, "run",
                        lambda args: seen.setdefault("args", args) and type("R", (), {"returncode": 0}))
    host.run_elevated(["netsh", "add", "name=AutoAdmin Minecraft", "it's"])
    script = seen["args"][-1]
    assert "-FilePath 'netsh'" in script
    assert "-ArgumentList 'add \"name=AutoAdmin Minecraft\" it''s'" in script


# -- the wizard ----------------------------------------------------------------------------

class FakeIO:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.out: list[str] = []

    def _next(self, kind, prompt):
        self.out.append(f"{kind}: {prompt}")
        want, value = self.answers.pop(0)
        assert want == kind, f"expected {want}, got {kind} ({prompt})"
        return value

    def say(self, text=""):
        self.out.append(text)

    def heading(self, text):
        self.out.append(f"== {text}")

    def secret(self, prompt):
        return self._next("secret", prompt)

    def pause(self, prompt=""):
        return self._next("pause", prompt)

    def confirm(self, prompt, default):
        return self._next("confirm", prompt)

    def choose(self, prompt, options, default=0):
        return self._next("choose", prompt)


class FakeDiscord:
    intent_flags = [0, 1 << 19]       # off at first, on after the user fixes it
    guild_lists = [[], [{"id": G, "name": "Mine"}]]

    def __init__(self, token):
        if token != "good-token":
            raise AssertionError("only the valid token is used")
        self.token = token

    def me(self):
        return {"id": BOT, "username": "AutoAdmin"}

    def application(self):
        flags = FakeDiscord.intent_flags.pop(0) if len(FakeDiscord.intent_flags) > 1 \
            else FakeDiscord.intent_flags[0]
        return {"id": "app1", "flags": flags}

    def guilds(self):
        return FakeDiscord.guild_lists.pop(0) if len(FakeDiscord.guild_lists) > 1 \
            else FakeDiscord.guild_lists[0]

    def channels(self, gid):
        return [{"id": "201", "name": "general", "type": 0, "permission_overwrites": [
                    overwrite(G, deny=SEND)]},
                {"id": "202", "name": "minecraft", "type": 0, "permission_overwrites": []}]

    def guild(self, gid):
        return guild()

    def member(self, gid, uid):
        return member()


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "config.example.toml").write_text((REPO / "config.example.toml").read_text())
    FakeDiscord.intent_flags = [0, 1 << 19]
    FakeDiscord.guild_lists = [[], [{"id": G, "name": "Mine"}]]

    def fake_api(token):
        if token == "bad-token":
            api = FakeDiscord.__new__(FakeDiscord)
            api.me = lambda: (_ for _ in ()).throw(Unauthorized("no"))
            return api
        return FakeDiscord(token)

    monkeypatch.setattr(wizard, "DiscordApi", fake_api)
    monkeypatch.setattr(checks, "check_anthropic",
                        lambda key, models: (_ for _ in ()).throw(Unauthorized("no"))
                        if key == "bad-key" else [])
    calls = []
    monkeypatch.setattr(wizard.service, "write_bot_unit", lambda p: calls.append("unit") or Path("x.service"))
    monkeypatch.setattr(wizard.service, "start_bot_unit", lambda: calls.append("start") or (True, ""))
    monkeypatch.setattr(wizard.hostmod, "linger_enabled", lambda: False)
    monkeypatch.setattr(wizard.hostmod, "enable_linger", lambda: calls.append("linger") or (True, ""))
    monkeypatch.setattr(wizard.hostmod, "linux_firewall", lambda: "ufw")
    monkeypatch.setattr(wizard.hostmod, "run_interactive", lambda cmds: calls.append(cmds) or True)
    return tmp_path, calls


LINUX = host.Host("linux", "x64", True, "Arch Linux")


def test_wizard_linux_happy_path(project):
    root, calls = project
    io = FakeIO(
        ("secret", "bad-key"), ("secret", "good-key"),        # rejected, then accepted
        ("secret", "bad-token"), ("secret", "good-token"),
        ("pause", "intent"),                                  # intent off -> user turns it on
        ("pause", "invite"),                                  # bot in no server yet
        ("choose", 0),                                        # guild "Mine"
        ("choose", 0), ("pause", "fix"),                      # #general: can't send
        ("choose", 1),                                        # #minecraft
        ("confirm", True),                                    # systemd service
        ("confirm", True),                                    # linger
        ("confirm", False),                                   # firewall: declined
    )
    assert wizard.Wizard(root / "config.toml", io, LINUX).run() == 0
    assert not io.answers

    env = (root / ".env").read_text()
    assert "ANTHROPIC_API_KEY=good-key" in env and "AUTOADMIN_DISCORD_TOKEN=good-token" in env
    raw = tomllib.loads((root / "config.toml").read_text())["discord"]
    assert (raw["guild_id"], raw["channel_id"]) == (100, 202)
    assert calls == ["unit", "start", "linger"]
    text = "\n".join(io.out)
    assert "missing Send Messages in #general" in text
    assert "sudo ufw allow 25565/tcp" in text                  # printed when declined


def test_wizard_rerun_keeps_existing_values(project):
    root, calls = project
    (root / ".env").write_text("ANTHROPIC_API_KEY=good-key\nAUTOADMIN_DISCORD_TOKEN=good-token\n")
    FakeDiscord.intent_flags = [1 << 19]
    FakeDiscord.guild_lists = [[{"id": G, "name": "Mine"}]]
    io = FakeIO(("choose", 0), ("choose", 0),                  # keep both secrets
                ("choose", 0), ("choose", 1),                  # guild, channel
                ("confirm", False),                            # no service
                ("confirm", True))                             # open firewall
    assert wizard.Wizard(root / "config.toml", io, LINUX).run() == 0
    assert calls == [[["sudo", "ufw", "allow", "25565/tcp", "comment", "AutoAdmin Minecraft"]]]
    assert not any(o.startswith("secret") for o in io.out)


def test_wizard_refuses_unsupported_hosts(project):
    root, _ = project
    io = FakeIO()
    assert wizard.Wizard(root / "config.toml", io, host.Host("macos", "aarch64", False, "Darwin")).run() == 1
    assert wizard.Wizard(root / "config.toml", io, host.Host("linux", "x64", False, "Alpine")).run() == 1
    assert not (root / "config.toml").exists()
