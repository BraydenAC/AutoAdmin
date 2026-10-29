"""Writes the systemd --user unit that runs the Discord bot."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

from .server.systemd import UNIT_DIR

BOT_UNIT = "autoadmin-bot.service"


def render_bot_unit(config_path: Path, executable: Path) -> str:
    exec_start = shlex.join([str(executable), "discord", "--config", str(config_path)])
    return f"""\
# Managed by AutoAdmin (`autoadmin install-service`).
[Unit]
Description=AutoAdmin Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={config_path.parent}
ExecStart={exec_start}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""


def install_bot_service(config_path: Path) -> str:
    executable = Path(sys.executable).with_name("autoadmin")
    if not executable.exists():
        raise SystemExit(f"{executable} not found; run `uv sync` first")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    path = UNIT_DIR / BOT_UNIT
    path.write_text(render_bot_unit(config_path, executable))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return (f"Wrote {path}\n"
            f"Start now:          systemctl --user start {BOT_UNIT}\n"
            f"Start at login/boot: systemctl --user enable {BOT_UNIT}  (plus `loginctl enable-linger` for boot)\n"
            f"Logs:               journalctl --user -u {BOT_UNIT} -f")
