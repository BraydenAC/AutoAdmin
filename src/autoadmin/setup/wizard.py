"""`autoadmin setup`: guided first-time setup. Safe to re-run to rotate keys or
change the channel; every existing value can be kept."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .. import service
from ..config import Config
from . import checks, host as hostmod
from .checks import CheckError, DiscordApi, Unauthorized, Unreachable
from .console import Console
from .files import EnvFile, ensure_config, set_discord_ids

ANTHROPIC_ENV = "ANTHROPIC_API_KEY"
PORTAL = "https://discord.com/developers/applications"


class SetupAborted(Exception):
    pass


class Wizard:
    def __init__(self, config_path: Path, io: Console | None = None,
                 host: hostmod.Host | None = None):
        self.config_path = config_path.resolve()
        self.io = io or Console()
        self.host = host or hostmod.detect()
        self.config: Config | None = None
        self.env: EnvFile | None = None

    def run(self) -> int:
        io, h = self.io, self.host
        io.heading("AutoAdmin setup")
        io.say(f"Detected {h.describe()}.")
        if h.os not in ("linux", "windows"):
            io.say("AutoAdmin runs on Linux and Windows only.")
            return 1
        if h.os == "linux" and not h.systemd:
            io.say("AutoAdmin needs systemd on Linux to run the bot and the Minecraft server.")
            return 1
        if h.arch != "x64":
            io.say(f"Note: Java downloads currently support x64 only, not {h.arch}.")
        if h.os == "windows":
            io.say("Note: running the Minecraft server on Windows isn't supported yet. This sets up "
                   "the bot, keys, and firewall, and is ready for when it is.")

        if ensure_config(self.config_path):
            io.say(f"Created {self.config_path.name} from the example.")
        self.config = Config.load(self.config_path)
        self.env = EnvFile(self.config_path.parent / ".env")

        try:
            self.anthropic_key()
            api, app, bot = self.discord_token()
            self.message_content_intent(api, app)
            guild_id = self.pick_guild(api, app)
            channel = self.pick_channel(api, guild_id, bot["id"])
        except SetupAborted as e:
            io.say(f"Setup stopped: {e}. Anything already saved is kept; run setup again to finish.")
            return 1
        set_discord_ids(self.config_path, int(guild_id), int(channel["id"]))
        io.say(f"Saved the server and #{channel['name']} to {self.config_path.name}.")

        started = self.background_service()
        if h.os == "linux" and started:
            self.linger()
        self.firewall(self.config.server.server_port)

        io.heading("Done")
        if started:
            io.say(f"The bot is starting. @mention it in #{channel['name']} to set up a server.")
        else:
            io.say("Start the bot with `uv run autoadmin discord`, then @mention it in "
                   f"#{channel['name']} to set up a server.")
        return 0

    # -- secrets ------------------------------------------------------------------------

    def _secret(self, key: str, label: str, validate: Callable[[str], bool]) -> str:
        """Keep or replace an existing value; loop until a value validates."""
        value = self.env.get(key)
        if value and self.io.choose(f"{label} is already set (ends in …{value[-4:]}).",
                                    ["Keep it (it will be re-checked)", "Enter a new one"]) == 1:
            value = None
        while True:
            if not value:
                value = self.io.secret(label)
                if not value:
                    continue
            if validate(value):
                self.env.set(key, value)
                self.env.save()
                return value
            value = None

    def _unreachable(self, e: Exception) -> bool:
        self.io.say(f"Couldn't check it: {e}.")
        return self.io.confirm("Save it without checking?", default=False)

    def anthropic_key(self) -> str:
        self.io.heading("Anthropic API key")
        models = list(dict.fromkeys([self.config.agent.model, self.config.investigator.model]))

        def validate(key: str) -> bool:
            try:
                missing = checks.check_anthropic(key, models)
            except Unauthorized:
                self.io.say("That key was rejected. Create one at https://console.anthropic.com/settings/keys.")
                return False
            except Unreachable as e:
                return self._unreachable(e)
            except CheckError as e:
                self.io.say(f"Couldn't check the key: {e}.")
                return False
            if missing:
                self.io.say(f"The key works, but it can't use {', '.join(missing)}. Change the "
                            f"model in {self.config_path.name} ([agent] / [investigator]).")
            else:
                self.io.say("The key works.")
            return True

        return self._secret(ANTHROPIC_ENV, "Anthropic API key", validate)

    def discord_token(self) -> tuple[DiscordApi, dict, dict]:
        io = self.io
        io.heading("Discord bot")
        token_env = self.config.discord.token_env
        if not self.env.get(token_env):
            io.say(f"1. Open {PORTAL} and create an application (or open yours).")
            io.say("2. On the Bot page, turn on Message Content Intent and save.")
            io.say("3. Click Reset Token and copy the token.")
        found: dict = {}

        def validate(token: str) -> bool:
            api = DiscordApi(token)
            while True:
                try:
                    found.update(api=api, bot=api.me(), app=api.application())
                    io.say(f"Connected as {found['bot']['username']}.")
                    return True
                except Unauthorized:
                    io.say("Discord rejected that token. Reset it on the Bot page and copy it again.")
                    return False
                except CheckError as e:
                    # Setup can't pick a server without Discord, so no "save anyway".
                    io.say(f"Couldn't check the token: {e}.")
                    if not io.confirm("Try again?", default=True):
                        raise SetupAborted("Discord is unreachable")

        self._secret(token_env, "Discord bot token", validate)
        return found["api"], found["app"], found["bot"]

    # -- Discord connection ------------------------------------------------------------------

    def message_content_intent(self, api: DiscordApi, app: dict) -> None:
        while not checks.has_message_content(app):
            self.io.say("Message Content Intent is off, so the bot couldn't read messages. Turn it "
                        f"on at {PORTAL}/{app['id']}/bot and save.")
            self.io.pause("Press Enter when it's on")
            app = api.application()
        self.io.say("Message Content Intent is on.")

    def pick_guild(self, api: DiscordApi, app: dict) -> str:
        io, invite = self.io, checks.invite_url(app["id"])
        current = str(self.config.discord.guild_id)
        while True:
            guilds = api.guilds()
            if not guilds:
                io.say(f"Add the bot to your Discord server with this link:\n  {invite}")
                io.pause("Press Enter once it's added")
                continue
            names = [g["name"] + (" (current)" if g["id"] == current else "") for g in guilds]
            default = next((i for i, g in enumerate(guilds) if g["id"] == current), 0)
            i = io.choose("Which Discord server should AutoAdmin run in?",
                          [*names, "Add the bot to another server"], default)
            if i < len(guilds):
                return guilds[i]["id"]
            io.say(f"Invite link:\n  {invite}")
            io.pause("Press Enter once it's added")

    def pick_channel(self, api: DiscordApi, guild_id: str, bot_id: str) -> dict:
        io = self.io
        current = str(self.config.discord.channel_id)
        while True:
            channels = checks.text_channels(api.channels(guild_id))
            if not channels:
                io.say("That server has no text channels the bot can see. Create one for AutoAdmin.")
                io.pause("Press Enter to look again")
                continue
            names = ["#" + c["name"] + (" (current)" if c["id"] == current else "") for c in channels]
            default = next((i for i, c in enumerate(channels) if c["id"] == current), 0)
            channel = channels[io.choose("Which channel should AutoAdmin use? (a dedicated one is best)",
                                        names, default)]
            # Roles and overwrites can change between attempts, so fetch fresh each time.
            perms = checks.channel_permissions(api.guild(guild_id), api.member(guild_id, bot_id),
                                               channel)
            if not (missing := checks.missing_permissions(perms)):
                return channel
            io.say(f"The bot is missing {', '.join(missing)} in #{channel['name']}. Give its role "
                   "those permissions in that channel, or pick another channel.")
            io.pause("Press Enter to check again")

    # -- host ------------------------------------------------------------------------------

    def background_service(self) -> bool:
        """Returns True if the bot was set to run in the background and started."""
        io = self.io
        io.heading("Running in the background")
        if self.host.os == "linux":
            if not io.confirm("Run the bot as a systemd user service and start it now?", default=True):
                return False
            path = service.write_bot_unit(self.config_path)
            ok, err = service.start_bot_unit()
            io.say(f"Installed {path.name} and started it." if ok else f"Couldn't start it: {err}")
            if ok:
                io.say(f"Logs: journalctl --user -u {service.BOT_UNIT} -f")
            return ok

        mode = io.choose("When should the bot run?", [
            "Only while I'm logged in (no admin needed)",
            "Always, even when nobody is logged in (asks for admin once)",
            "Don't run it in the background",
        ])
        if mode == 2:
            return False
        log_file = self.config.runtime / "logs" / "bot.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        ok, note = service.install_bot_task(self.config_path, log_file, always_on=mode == 1)
        if not ok:
            io.say(f"Couldn't set it up: {note}.")
            return False
        io.say(f"Registered the '{service.BOT_TASK}' task" + (f" ({note})." if note else " and started it."))
        io.say(f"Logs: {log_file}")
        return not note

    def linger(self) -> None:
        io = self.io
        if hostmod.linger_enabled():
            return
        io.say("Without lingering, systemd stops the bot and the server when you log out.")
        if not io.confirm("Keep them running after logout and start them at boot?", default=True):
            return
        ok, out = hostmod.enable_linger()
        io.say("Lingering is on." if ok else
               f"Couldn't enable it ({out}). Run `sudo loginctl enable-linger $USER` yourself.")

    def firewall(self, port: int) -> None:
        io = self.io
        io.heading("Firewall")
        if self.host.os == "linux":
            kind = hostmod.linux_firewall()
            if kind is None:
                io.say("No ufw or firewalld is running, so there's no firewall port to open.")
                return
            commands = hostmod.linux_firewall_commands(kind, port)
            if io.confirm(f"{kind} is running. Open port {port}/tcp so other devices can join? "
                          "(uses sudo)", default=False):
                if hostmod.run_interactive(commands):
                    io.say(f"Opened {port}/tcp.")
                    return
                io.say("That didn't work.")
            io.say("To open it yourself:\n" + "\n".join("  " + " ".join(c) for c in commands))
            return

        if hostmod.windows_firewall_rule_exists():
            io.say(f"Windows Firewall already has the '{hostmod.FIREWALL_RULE}' rule.")
            return
        command = hostmod.windows_firewall_command(port)
        if io.confirm(f"Open port {port}/tcp in Windows Firewall so other devices can join? "
                      "(asks for admin)", default=False):
            if hostmod.run_elevated(command) == 0:
                io.say(f"Opened {port}/tcp.")
                return
            io.say("That didn't work.")
        io.say("To open it yourself, in an administrator terminal:\n  " +
               " ".join(f'"{a}"' if " " in a else a for a in command))
