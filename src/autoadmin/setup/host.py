"""Host detection and the OS-level steps of setup: linger, firewall, elevation."""

from __future__ import annotations

import getpass
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FIREWALL_RULE = "AutoAdmin Minecraft"


@dataclass(frozen=True)
class Host:
    os: str          # linux | windows | macos | other
    arch: str        # x64 | aarch64 | raw machine name
    systemd: bool
    name: str        # human-readable, e.g. "Arch Linux"

    def describe(self) -> str:
        return f"{self.name} ({self.arch})"


def detect() -> Host:
    system = platform.system()
    os_name = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}.get(system, system.lower())
    machine = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        machine, machine)
    name = system
    if os_name == "linux":
        try:
            name = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
        except OSError:
            name = "Linux"
    elif os_name == "windows":
        name = f"Windows {platform.release()}"
    return Host(os_name, arch, os_name == "linux" and Path("/run/systemd/system").is_dir(), name)


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


# -- Linux -----------------------------------------------------------------------------

def linger_enabled() -> bool:
    out = run("loginctl", "show-user", getpass.getuser(), "-p", "Linger").stdout
    return out.strip() == "Linger=yes"


def enable_linger() -> tuple[bool, str]:
    # Polkit lets a user enable linger for themselves on most distros; no sudo.
    r = run("loginctl", "enable-linger")
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def linux_firewall() -> str | None:
    """The active firewall manager, if any. Checked without root."""
    for kind, unit in (("ufw", "ufw"), ("firewalld", "firewalld")):
        if shutil.which("ufw" if kind == "ufw" else "firewall-cmd") and \
                run("systemctl", "is-active", unit).stdout.strip() == "active":
            return kind
    return None


def linux_firewall_commands(kind: str, port: int) -> list[list[str]]:
    if kind == "ufw":
        return [["sudo", "ufw", "allow", f"{port}/tcp", "comment", FIREWALL_RULE]]
    return [["sudo", "firewall-cmd", "--permanent", f"--add-port={port}/tcp"],
            ["sudo", "firewall-cmd", "--reload"]]


def run_interactive(commands: list[list[str]]) -> bool:
    """Run with the terminal attached, so sudo can ask for a password."""
    return all(subprocess.run(cmd).returncode == 0 for cmd in commands)


# -- Windows -----------------------------------------------------------------------------

def windows_firewall_command(port: int) -> list[str]:
    return ["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}",
            "dir=in", "action=allow", "protocol=TCP", f"localport={port}"]


def windows_firewall_rule_exists() -> bool:
    return run("netsh", "advfirewall", "firewall", "show", "rule",
               f"name={FIREWALL_RULE}").returncode == 0


def ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def run_elevated(args: list[str]) -> int:
    """Run one command as administrator through a UAC prompt and wait for it.
    Returns its exit code (1223 if the user declined the prompt)."""
    script = (f"try {{ $p = Start-Process -FilePath {ps_quote(args[0])} "
              f"-ArgumentList {ps_quote(subprocess.list2cmdline(args[1:]))} "
              "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode } "
              "catch { exit 1223 }")
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                           script]).returncode
