"""Failure excerpts and incident records.

The main agent only ever sees an excerpt: the last stack trace plus a few
lines of context. The full log stays on disk for the investigator.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path

CONTEXT_LINES = 10
MAX_EXCERPT_LINES = 45
MAX_EXCERPT_CHARS = 4000

_TRACE_LINE = re.compile(r"^\s+(at |\.\.\. \d+ more)")
_EXCEPTION = re.compile(r"(Exception|Error|Throwable)\b.*|^Caused by: ")


def excerpt_failure(lines: list[str]) -> str:
    """The last stack trace with context around it, or the tail if there is none."""
    trace_end = None
    for i in range(len(lines) - 1, -1, -1):
        if _TRACE_LINE.match(lines[i]):
            trace_end = i
            break
    if trace_end is None:
        chosen = lines[-(CONTEXT_LINES + 5):]
    else:
        start = trace_end
        # Walk back over the trace and its "Caused by" headers.
        while start > 0 and (_TRACE_LINE.match(lines[start - 1])
                             or _EXCEPTION.search(lines[start - 1])):
            start -= 1
        lo = max(0, start - CONTEXT_LINES)
        hi = min(len(lines), trace_end + 1 + CONTEXT_LINES)
        chosen = lines[lo:hi]
        # Long traces: keep the head (exception + first frames) and the context after.
        if len(chosen) > MAX_EXCERPT_LINES:
            head = MAX_EXCERPT_LINES - CONTEXT_LINES - 1
            chosen = chosen[:head] + ["    ... (trace trimmed) ..."] + chosen[-CONTEXT_LINES:]
    text = "\n".join(line.rstrip() for line in chosen)
    if len(text) > MAX_EXCERPT_CHARS:
        text = text[:MAX_EXCERPT_CHARS] + "\n... (trimmed)"
    return text


def read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []


@dataclass
class Incident:
    id: str
    ts: float
    kind: str          # install_failed | start_failed | start_timeout | crash
    summary: str
    excerpt: str
    log_files: list[str]   # jail-relative paths the investigator may read

    def brief(self) -> str:
        return f"{self.summary}\nincident_id: {self.id}\n```\n{self.excerpt}\n```"


class IncidentStore:
    def __init__(self, root: Path, runtime: Path):
        self.root = root
        self.runtime = runtime
        root.mkdir(parents=True, exist_ok=True)

    def create(self, kind: str, summary: str, excerpt: str, log_files: list[Path]) -> Incident:
        inc = Incident(
            id=time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
            ts=time.time(), kind=kind, summary=summary, excerpt=excerpt,
            log_files=[str(p.relative_to(self.runtime)) for p in log_files if p.exists()],
        )
        (self.root / f"{inc.id}.json").write_text(json.dumps(asdict(inc), indent=2))
        return inc

    def get(self, incident_id: str) -> Incident | None:
        if not re.fullmatch(r"[\w-]+", incident_id):
            return None
        p = self.root / f"{incident_id}.json"
        return Incident(**json.loads(p.read_text())) if p.exists() else None
