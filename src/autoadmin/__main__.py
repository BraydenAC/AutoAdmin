"""Entry point: `autoadmin cli [--config PATH]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .config import Config


def main() -> None:
    parser = argparse.ArgumentParser(prog="autoadmin")
    parser.add_argument("mode", choices=["cli", "discord", "install-service"],
                        help="cli: terminal test harness; discord: run the bot; "
                             "install-service: write the bot's systemd user unit")
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    default_level = logging.INFO if args.mode == "discord" else logging.WARNING
    logging.basicConfig(level=logging.DEBUG if args.verbose else default_level,
                        format="%(levelname)s %(name)s: %(message)s")
    if not args.config.exists():
        parser.error(f"{args.config} not found; copy config.example.toml to config.toml")
    config = Config.load(args.config)
    # Secrets live in .env beside the config; real environment variables win.
    load_dotenv(args.config.resolve().parent / ".env", override=False)

    if args.mode == "install-service":
        from .service import install_bot_service
        print(install_bot_service(args.config.resolve()))
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        parser.error("ANTHROPIC_API_KEY is not set; put it in .env")

    if args.mode == "cli":
        from . import cli
        asyncio.run(cli.run(config))
    elif args.mode == "discord":
        token = os.environ.get(config.discord.token_env)
        if not token:
            parser.error(f"{config.discord.token_env} is not set; put it in .env")
        from . import discord_bot
        discord_bot.run(config, token)


if __name__ == "__main__":
    main()
