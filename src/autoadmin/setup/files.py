"""The two files setup writes: `.env` (secrets) and `config.toml` (Discord IDs)."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tomllib
from pathlib import Path

from dotenv import dotenv_values

from ..fileperm import restrict_to_owner


class EnvFile:
    """Edits KEY=value lines in place, keeping everything else in the file."""

    def __init__(self, path: Path):
        self.path = path
        self.lines = path.read_text().splitlines() if path.exists() else []

    def get(self, key: str) -> str | None:
        values = dotenv_values(stream=io.StringIO("\n".join(self.lines)))
        return values.get(key) or None

    def set(self, key: str, value: str) -> None:
        pattern = re.compile(rf"^\s*(export\s+)?{re.escape(key)}\s*=")
        line = f"{key}={value}"
        for i, existing in enumerate(self.lines):
            if pattern.match(existing):
                self.lines[i] = line
                return
        self.lines.append(line)

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        # Created owner-only, so the secret is never briefly world-readable.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(self.lines) + "\n")
        os.replace(tmp, self.path)
        restrict_to_owner(self.path)


def ensure_config(path: Path) -> bool:
    """Create config.toml from the example beside it. Returns True if created."""
    if path.exists():
        return False
    example = path.with_name("config.example.toml")
    if not example.exists():
        raise SystemExit(f"{example} not found; run setup from the AutoAdmin folder")
    shutil.copyfile(example, path)
    return True


DISCORD_SECTION = re.compile(r"(?ms)^\[discord\][ \t]*\n(.*?)(?=^\[|\Z)")


def set_discord_ids(path: Path, guild_id: int, channel_id: int) -> None:
    text = path.read_text()
    values = {"guild_id": guild_id, "channel_id": channel_id}
    m = DISCORD_SECTION.search(text)
    if m is None:
        text = text.rstrip("\n") + "\n\n[discord]\n" + "".join(
            f"{k} = {v}\n" for k, v in values.items())
    else:
        section = m.group(1)
        for key, value in values.items():
            line = re.compile(rf"(?m)^{key}[ \t]*=.*$")
            if line.search(section):
                section = line.sub(f"{key} = {value}", section)
            else:
                section = f"{key} = {value}\n" + section
        text = text[:m.start(1)] + section + text[m.end(1):]
    discord = tomllib.loads(text).get("discord", {})
    if discord.get("guild_id") != guild_id or discord.get("channel_id") != channel_id:
        raise RuntimeError(f"couldn't update [discord] in {path}; set the IDs by hand")
    path.write_text(text)


class SetupRecord:
    """What setup changed outside AutoAdmin's own files (firewall, linger), so
    uninstall undoes exactly that and nothing the user set up themselves."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return {}

    def update(self, **changes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({**self.load(), **changes}, indent=2))
