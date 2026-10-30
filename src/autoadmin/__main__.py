"""Entry point: `autoadmin {setup,cli,discord,install-service} [--config PATH]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(prog="autoadmin")
    parser.add_argument("mode", choices=["setup", "cli", "discord", "install-service"],
                        help="setup: guided first-time setup (keys, Discord, background service); "
                             "cli: terminal test harness; discord: run the bot; "
                             "install-service: write the bot's systemd user unit")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--log-file", type=Path,
                        help="log to this file instead of stderr (used by the Windows task)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    default_level = logging.INFO if args.mode == "discord" else logging.WARNING
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else default_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
                        if args.log_file else "%(levelname)s %(name)s: %(message)s",
                        filename=args.log_file, encoding="utf-8")

    if args.mode == "setup":
        from .setup import Wizard
        try:
            sys.exit(Wizard(args.config).run())
        except (KeyboardInterrupt, EOFError):
            print("\nSetup cancelled. Anything already saved is kept.")
            sys.exit(130)

    if not args.config.exists():
        parser.error(f"{args.config} not found; run `autoadmin setup` first")
    config = Config.load(args.config)
    # Secrets live in .env beside the config; real environment variables win.
    load_dotenv(args.config.resolve().parent / ".env", override=False)

    if args.mode == "install-service":
        if sys.platform == "win32":
            parser.error("on Windows, use `autoadmin setup` to run the bot in the background")
        from .service import install_bot_service
        print(install_bot_service(args.config.resolve()))
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("ANTHROPIC_API_KEY is not set; run `autoadmin setup`")

    if args.mode == "cli":
        from . import cli
        asyncio.run(cli.run(config))
    elif args.mode == "discord":
        token = os.environ.get(config.discord.token_env)
        if not token:
            parser.error(f"{config.discord.token_env} is not set; run `autoadmin setup`")
        from . import discord_bot
        try:
            discord_bot.run(config, token)
        except Exception:
            # Without a console (Windows task), the traceback would otherwise be lost.
            logging.getLogger("autoadmin").exception("bot crashed")
            raise


if __name__ == "__main__":
    main()
