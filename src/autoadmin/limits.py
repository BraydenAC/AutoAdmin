"""Per-user rate limits, so one person can't flood the bot or the server.

- Messages: per minute and per hour. Each refused message would otherwise cost a
  reply, so the user is told once per lockout and later messages are ignored.
- Requests in progress: how many of a user's messages may be running or queued
  at once (opening many conversations in parallel).
- Heavy actions (installs, world rebuilds, restarts) per hour, checked when the
  tool runs, so the agent can tell the user plainly.

The owner, admins and the system principal are exempt (configurable). Counters
live in memory; a bot restart resets them.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from .auth import PermissionStore, Principal
from .config import LimitsConfig

# Disruptive or expensive (downloads, disk, players kicked): tool -> actions.
HEAVY: dict[str, set[str]] = {
    "worlds": {"create", "import", "restore", "backup"},
    "mods": {"install", "install_url", "install_upload"},
    "server_power": {"start", "stop", "restart"},
}
MINUTE, HOUR = 60.0, 3600.0


def heavy_action(tool: str, args: dict) -> str | None:
    action = args.get("action")
    return f"{tool}.{action}" if action in HEAVY.get(tool, ()) else None


def _prune(events: deque[float], now: float) -> None:
    """Every window is at most an hour, so older events never matter again."""
    while events and events[0] <= now - HOUR:
        events.popleft()


def _wait(events: deque[float], limit: int, window: float, now: float) -> float:
    """Seconds until another event fits in the window (0 if it fits now)."""
    recent = [t for t in events if t > now - window]
    if limit <= 0 or len(recent) < limit:
        return 0.0
    return recent[-limit] + window - now


class RateLimiter:
    def __init__(self, cfg: LimitsConfig, perms: PermissionStore,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg, self.perms, self.clock = cfg, perms, clock
        self._messages: dict[str, deque[float]] = defaultdict(deque)
        self._heavy: dict[str, deque[float]] = defaultdict(deque)
        self._in_flight: dict[str, int] = defaultdict(int)
        self._told_until: dict[str, float] = {}

    def exempt(self, p: Principal) -> bool:
        return p.is_system or (self.cfg.exempt_admins and self.perms.is_privileged(p))

    # -- messages ----------------------------------------------------------------------------

    def admit(self, p: Principal) -> tuple[bool, str | None]:
        """Whether to handle a message now. When refused, also the one-time notice
        to post (None if the user was already told)."""
        if self.exempt(p):
            return True, None
        now = self.clock()
        events = self._messages[p.id]
        _prune(events, now)
        wait = max(_wait(events, self.cfg.messages_per_minute, MINUTE, now),
                   _wait(events, self.cfg.messages_per_hour, HOUR, now))
        if wait:
            return False, self._tell(p, now + wait,
                                     f"{p.name}, you're sending messages faster than I can take "
                                     f"them. Try again in {_human(wait)}.")
        if self.cfg.requests_in_progress and \
                self._in_flight[p.id] >= self.cfg.requests_in_progress:
            return False, self._tell(p, now + MINUTE,
                                     f"{p.name}, I'm still working on your earlier request. "
                                     "Send this again once I've answered.")
        events.append(now)
        self._told_until.pop(p.id, None)
        return True, None

    def _tell(self, p: Principal, until: float, text: str) -> str | None:
        if self._told_until.get(p.id, 0) > self.clock():
            return None
        self._told_until[p.id] = until
        return text

    def started(self, p: Principal) -> None:
        self._in_flight[p.id] += 1

    def finished(self, p: Principal) -> None:
        self._in_flight[p.id] = max(0, self._in_flight[p.id] - 1)
        if not self._in_flight[p.id]:
            self._told_until.pop(p.id, None)     # free again: a new refusal gets a notice

    # -- heavy actions -----------------------------------------------------------------------

    def heavy_wait(self, p: Principal) -> float:
        if self.exempt(p):
            return 0.0
        now = self.clock()
        _prune(self._heavy[p.id], now)
        return _wait(self._heavy[p.id], self.cfg.heavy_actions_per_hour, HOUR, now)

    def record_heavy(self, p: Principal) -> None:
        if not self.exempt(p):
            self._heavy[p.id].append(self.clock())


def _human(seconds: float) -> str:
    seconds = max(1, round(seconds))
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{round(seconds / 60)} minutes"
