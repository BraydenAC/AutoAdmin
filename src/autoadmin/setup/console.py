"""Terminal prompts for setup. Tests swap in a scripted fake."""

from __future__ import annotations

import getpass
import os
import sys
import webbrowser


def has_browser() -> bool:
    """A graphical browser can open here. Over SSH or on a headless Linux box,
    webbrowser would fall back to a text browser and take over the terminal."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


class Console:
    def say(self, text: str = "") -> None:
        print(text)

    def heading(self, text: str) -> None:
        print(f"\n== {text} ==")

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt}: ").strip()

    def open_url(self, url: str) -> bool:
        if not has_browser():
            return False
        try:
            return webbrowser.open(url)
        except Exception:      # no usable browser
            return False

    def pause(self, prompt: str = "Press Enter to continue") -> None:
        input(f"{prompt}… ")

    def confirm(self, prompt: str, default: bool) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = input(f"{prompt} [{hint}] ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")

    def choose(self, prompt: str, options: list[str], default: int = 0) -> int:
        print(prompt)
        for i, opt in enumerate(options, 1):
            print(f"  {i}. {opt}")
        while True:
            answer = input(f"Choose 1-{len(options)} [{default + 1}]: ").strip()
            if not answer:
                return default
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return int(answer) - 1
