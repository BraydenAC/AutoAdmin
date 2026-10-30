"""Runs the Discord bot in the background: a systemd --user unit on Linux, a Task
Scheduler task on Windows."""

from __future__ import annotations

import getpass
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from .server.systemd import UNIT_DIR

BOT_UNIT = "autoadmin-bot.service"
BOT_TASK = "AutoAdmin Bot"


# -- Linux -------------------------------------------------------------------------------

def render_bot_unit(config_path: Path, executable: Path) -> str:
    exec_start = shlex.join([str(executable), "discord", "--config", str(config_path)])
    return f"""\
# Managed by AutoAdmin (`autoadmin setup` / `autoadmin install-service`).
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


def write_bot_unit(config_path: Path) -> Path:
    executable = Path(sys.executable).with_name("autoadmin")
    if not executable.exists():
        raise SystemExit(f"{executable} not found; run `uv sync` first")
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    path = UNIT_DIR / BOT_UNIT
    path.write_text(render_bot_unit(config_path, executable))
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    return path


def install_bot_service(config_path: Path) -> str:
    path = write_bot_unit(config_path)
    return (f"Wrote {path}\n"
            f"Start now:          systemctl --user start {BOT_UNIT}\n"
            f"Start at login/boot: systemctl --user enable {BOT_UNIT}  (plus `loginctl enable-linger` for boot)\n"
            f"Logs:               journalctl --user -u {BOT_UNIT} -f")


def start_bot_unit() -> tuple[bool, str]:
    """Enable at login and (re)start, so new keys take effect."""
    for verb in ("enable", "restart"):
        r = subprocess.run(["systemctl", "--user", verb, BOT_UNIT], capture_output=True, text=True)
        if r.returncode:
            return False, (r.stdout + r.stderr).strip()
    return True, ""


# -- Windows -------------------------------------------------------------------------------

def windows_user() -> str:
    domain = os.environ.get("USERDOMAIN")
    return f"{domain}\\{getpass.getuser()}" if domain else getpass.getuser()


def bot_command_windows(config_path: Path, log_file: Path) -> tuple[Path, list[str]]:
    """pythonw has no console window; logs go to a file instead."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return (pythonw if pythonw.exists() else exe,
            ["-m", "autoadmin", "discord", "--config", str(config_path),
             "--log-file", str(log_file)])


def render_bot_task(*, user: str, command: Path, arguments: list[str], workdir: Path,
                    always_on: bool) -> str:
    """Task Scheduler XML. Logged-in mode runs at the user's logon with their session
    token (no admin). Always-on runs at boot as the user via S4U, which needs admin to
    register but stores no password; it can reach the internet, not network shares."""
    u = escape(user)
    trigger = ("<BootTrigger><Enabled>true</Enabled><Delay>PT1M</Delay></BootTrigger>"
               if always_on else
               f"<LogonTrigger><Enabled>true</Enabled><UserId>{u}</UserId>"
               "<Delay>PT30S</Delay></LogonTrigger>")
    logon = "S4U" if always_on else "InteractiveToken"
    return f"""\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>AutoAdmin Discord bot (managed by autoadmin setup)</Description></RegistrationInfo>
  <Triggers>{trigger}</Triggers>
  <Principals>
    <Principal id="Author"><UserId>{u}</UserId><LogonType>{logon}</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <Arguments>{escape(subprocess.list2cmdline(arguments))}</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_bot_task(config_path: Path, log_file: Path, *, always_on: bool) -> tuple[bool, str]:
    """Register (or replace) the task and start it. Always-on asks for admin once."""
    from .setup.host import run_elevated

    command, arguments = bot_command_windows(config_path, log_file)
    xml = render_bot_task(user=windows_user(), command=command, arguments=arguments,
                          workdir=config_path.parent, always_on=always_on)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "autoadmin-bot.xml"
        path.write_text(xml, encoding="utf-16")
        create = ["schtasks", "/Create", "/TN", BOT_TASK, "/XML", str(path), "/F"]
        if always_on:
            code, out = run_elevated(create), ""
        else:
            r = subprocess.run(create, capture_output=True, text=True)
            code, out = r.returncode, (r.stdout + r.stderr).strip()
    if code == 1223:
        return False, "the administrator prompt was declined"
    if code:
        return False, out or f"schtasks exited with code {code}"
    # Restart so new keys take effect.
    subprocess.run(["schtasks", "/End", "/TN", BOT_TASK], capture_output=True)
    r = subprocess.run(["schtasks", "/Run", "/TN", BOT_TASK], capture_output=True, text=True)
    if r.returncode:
        return True, f"registered, but couldn't start it now: {(r.stdout + r.stderr).strip()}"
    return True, ""
