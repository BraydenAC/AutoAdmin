"""Append-only audit log of every tool call."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

MAX_FIELD = 2000


def _clip(value: Any) -> Any:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    return s if len(s) <= MAX_FIELD else s[:MAX_FIELD] + "…"


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, *, principal: str, tool: str, args: dict, allowed: bool,
               result: str, error: bool = False) -> None:
        entry = {
            "ts": time.time(),
            "principal": principal,
            "tool": tool,
            "args": _clip(args),
            "allowed": allowed,
            "error": error,
            "result": _clip(result),
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
