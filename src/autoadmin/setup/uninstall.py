"""`autoadmin uninstall`: undo everything setup and the bot did on this machine.

Order matters:
1. The bot's background service goes first, so its supervisor can't restart the server.
2. The server is stopped with `stop`, so each world is saved.
3. Worlds are copied to Downloads if wanted. If that fails, nothing is deleted.
4. The bot leaves the Discord server, while the token is still at hand.
5. The firewall rule and linger are undone if setup turned them on (setup.json).
6. Finally runtime/, .env and config.toml are deleted. The code stays; its path is printed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import time
from pathlib import Path

from .. import service
from ..config import Config, DiscordConfig, ServerConfig
from ..server import properties
from ..server.backend import make_backend
from ..server.transfer import downloads_dir, export_world
from . import host as hostmod
from .checks import CheckError, DiscordApi, Unauthorized
from .console import Console
from .files import EnvFile, SetupRecord
from .wizard import PORTAL, SETUP_RECORD

def project_dir(config_path: Path) -> Path:
    repo = Path(__file__).resolve().parents[3]     # src/autoadmin/setup -> repo root
    return repo if (repo / "pyproject.toml").exists() else config_path.parent


def _rmtree(path: Path) -> None:
    def retry(func, p, _exc):          # Windows: read-only files (e.g. in the Java runtime)
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onexc=retry)


class Uninstaller:
    def __init__(self, config_path: Path, io: Console | None = None,
                 host: hostmod.Host | None = None):
        self.config_path = config_path.resolve()
        self.io = io or Console()
        self.host = host or hostmod.detect()
        if self.config_path.exists():
            cfg = Config.load(self.config_path)
            self.runtime, self.server_cfg, self.discord_cfg = cfg.runtime, cfg.server, cfg.discord
        else:
            self.runtime = self.config_path.parent / "runtime"
            self.server_cfg, self.discord_cfg = ServerConfig(), DiscordConfig()
        self.env_path = self.config_path.parent / ".env"
        self.record = SetupRecord(self.runtime / SETUP_RECORD).load()

    def worlds(self) -> list[Path]:
        root = self.runtime / "worlds"
        return sorted(p for p in root.iterdir() if (p / "autoadmin.json").exists()) \
            if root.is_dir() else []

    def run(self) -> int:
        io = self.io
        io.heading("Uninstall AutoAdmin")
        worlds = self.worlds()
        io.say("This removes AutoAdmin from this computer:\n"
               "  - stops the bot and the Minecraft server and removes their background service\n"
               "  - removes the bot from your Discord server\n"
               "  - closes the firewall port and turns off lingering, if setup turned them on\n"
               f"  - deletes all worlds, backups, downloaded Java, and logs ({self.runtime})\n"
               "  - deletes your saved keys (.env) and config.toml\n"
               "Before anything is deleted, you can save a copy of each world to Downloads.")
        io.say("Worlds: " + (", ".join(w.name for w in worlds) if worlds else "none"))
        io.say("If you started the bot yourself in a terminal, close it first.")
        if not io.confirm("Uninstall AutoAdmin?", default=False):
            io.say("Nothing was changed.")
            return 1

        self.remove_bot_service()
        if not self.stop_server():
            return 1
        if worlds and not self.rescue(worlds):
            return 1
        self.leave_discord()
        self.firewall()
        self.linger()
        self.delete_files()

        io.heading("Done")
        io.say(f"AutoAdmin's code is still in:\n  {project_dir(self.config_path)}\n"
               "Delete that folder to remove it completely (it includes the Python "
               "environment, .venv). uv and Python stay installed.")
        return 0

    # -- processes ------------------------------------------------------------------------

    def remove_bot_service(self) -> None:
        io = self.io
        io.heading("Background service")
        if self.host.os == "linux":
            removed = service.remove_unit(service.BOT_UNIT)
            io.say(f"Stopped and removed {service.BOT_UNIT}." if removed
                   else "The bot wasn't installed as a service.")
            return
        if not service.bot_task_exists():
            io.say("The bot wasn't installed as a scheduled task.")
            return
        ok, note = service.remove_bot_task()
        io.say(f"Stopped the bot and removed the '{service.BOT_TASK}' task." if ok else
               f"Couldn't remove the '{service.BOT_TASK}' task ({note}). Delete it in Task "
               "Scheduler yourself.")

    def stop_server(self) -> bool:
        io = self.io
        io.heading("Minecraft server")
        backend = make_backend(self.server_cfg.unit_name, self.runtime / "run",
                               self.server_cfg.stop_timeout_s)

        async def stop() -> bool:
            if not (await backend.state()).running:
                return False
            await backend.stop()
            return True
        if asyncio.run(stop()):
            io.say("Stopped the server and saved the world.")
        else:
            io.say("The server wasn't running.")
        if asyncio.run(backend.state()).running:
            io.say("The server is still running, so nothing was deleted. Stop it and run "
                   "uninstall again.")
            return False
        if self.host.os == "linux":
            service.remove_unit(f"{self.server_cfg.unit_name}.service")
        return True

    # -- data -----------------------------------------------------------------------------

    def rescue(self, worlds: list[Path]) -> bool:
        io = self.io
        io.heading("Keep your worlds")
        dest_dir = downloads_dir()
        if not io.confirm(f"Save a copy of each world ({len(worlds)}) to {dest_dir}?",
                          default=True):
            return True
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for w in worlds:
            dest = dest_dir / f"AutoAdmin-{w.name}-{stamp}.zip"
            try:
                export_world(w, dest)
            except OSError as e:
                io.say(f"Couldn't save '{w.name}': {e}. Nothing was deleted.")
                return False
            io.say(f"Saved {dest} ({dest.stat().st_size / 1024 ** 2:.0f} MB).")
        level = (properties.level_name(properties.read(worlds[0] / "server.properties"))
                 or properties.DEFAULT_LEVEL_NAME)
        io.say(f"Each zip holds the server's folder: settings, mods, and the world itself "
               f"(the '{level}' folder). To play it in single-player, copy that folder into "
               ".minecraft/saves.")
        return True

    def leave_discord(self) -> None:
        io = self.io
        io.heading("Discord")
        token = EnvFile(self.env_path).get(self.discord_cfg.token_env)
        guild = self.discord_cfg.guild_id
        if not token:
            io.say("No bot token is saved, so there's no bot to remove.")
            return
        api = DiscordApi(token)
        try:
            app = api.application()
            if guild and io.confirm("Remove the bot from your Discord server?", default=True):
                api.leave_guild(str(guild))
                io.say("The bot left your server.")
        except Unauthorized:
            io.say("Discord no longer accepts the saved token. To remove the bot, use Server "
                   "Settings > Integrations in Discord.")
            return
        except CheckError as e:
            io.say(f"Couldn't reach Discord ({e}). Remove the bot in Server Settings > "
                   "Integrations instead.")
            return
        io.say(f"The bot itself still exists. To delete it, open {PORTAL}/{app['id']}/information "
               'and click "Delete App".')

    def firewall(self) -> None:
        io = self.io
        port = self.record.get("port", self.server_cfg.server_port)
        opened = self.record.get("firewall")
        if self.host.os == "windows":
            if not hostmod.windows_firewall_rule_exists():
                return
            io.heading("Firewall")
            if io.confirm(f"Remove the '{hostmod.FIREWALL_RULE}' firewall rule? (asks for admin)",
                          default=True):
                ok = hostmod.run_elevated(hostmod.windows_firewall_remove_command()) == 0
                io.say("Removed it." if ok else "That didn't work; remove it in Windows "
                       "Defender Firewall yourself.")
            return
        kind = opened or hostmod.linux_firewall()
        if kind is None:
            return
        io.heading("Firewall")
        commands = hostmod.linux_firewall_remove_commands(kind, port)
        question = (f"Setup opened {port}/tcp in {kind}. Close it? (uses sudo)" if opened else
                    f"{kind} is running. If you opened {port}/tcp for AutoAdmin yourself, "
                    "close it now? (uses sudo)")
        if io.confirm(question, default=bool(opened)):
            if hostmod.run_interactive(commands):
                io.say(f"Closed {port}/tcp.")
                return
            io.say("That didn't work.")
        io.say("To close it yourself:\n" + "\n".join("  " + " ".join(c) for c in commands))

    def linger(self) -> None:
        if self.host.os != "linux" or not hostmod.linger_enabled():
            return
        io = self.io
        io.heading("Lingering")
        if self.record.get("linger"):
            if io.confirm("Setup turned on lingering (services keep running after logout). "
                          "Turn it off?", default=True):
                ok, out = hostmod.disable_linger()
                io.say("Lingering is off." if ok else f"Couldn't turn it off ({out}).")
            return
        io.say("Lingering is on, but setup didn't turn it on, so it's left as is "
               "(`loginctl disable-linger` turns it off).")

    def delete_files(self) -> None:
        io = self.io
        io.heading("Deleting files")
        for path in (self.runtime, self.env_path, self.config_path):
            if not path.exists():
                continue
            try:
                _rmtree(path) if path.is_dir() else path.unlink()
                io.say(f"Deleted {path}")
            except OSError as e:
                io.say(f"Couldn't delete {path}: {e}")
