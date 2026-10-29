"""Token/cost accounting, the global cap, and per-user spend limits."""

from __future__ import annotations

import logging
import sqlite3
import time
from enum import StrEnum
from pathlib import Path
from typing import Any

from .auth import PermissionStore, Principal
from .config import BudgetConfig, Price

log = logging.getLogger(__name__)
PER_TOKEN = 1 / 1_000_000


class BudgetEvent(StrEnum):
    WARNING = "warning"      # crossed warn_fraction of the cap
    EXHAUSTED = "exhausted"  # reached the cap -> no-AI mode


class Budget:
    def __init__(self, db_path: Path, cfg: BudgetConfig, pricing: dict[str, Price],
                 perms: PermissionStore):
        self.cfg = cfg
        self.pricing = pricing
        self.perms = perms
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS spend (
                ts REAL, principal TEXT, model TEXT,
                input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER,
                cost REAL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
        """)
        self.db.commit()

    # -- kv state ------------------------------------------------------------

    def _get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM kv WHERE key=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))
        self.db.commit()

    # -- cap -------------------------------------------------------------------

    @property
    def cap(self) -> float:
        override = self._get("cap_usd")
        return float(override) if override is not None else self.cfg.cap_usd

    def set_cap(self, usd: float) -> None:
        if usd < 0:
            raise ValueError("cap must be non-negative")
        self._set("cap_usd", repr(float(usd)))
        # Re-arm threshold notices that no longer apply under the new cap.
        spent = self.total()
        if spent < usd * self.cfg.warn_fraction:
            self._set("warned", None)
        if spent < usd:
            self._set("exhausted_notified", None)

    # -- pricing ---------------------------------------------------------------

    def _price(self, model: str) -> Price:
        if model in self.pricing:
            return self.pricing[model]
        log.warning("no pricing for %s; charging at the most expensive configured rate", model)
        return max(self.pricing.values(), key=lambda p: p.output)

    def _cost(self, model: str, u: Any) -> tuple[float, tuple[int, int, int, int]]:
        p = self._price(model)
        tokens = (
            getattr(u, "input_tokens", 0) or 0,
            getattr(u, "output_tokens", 0) or 0,
            getattr(u, "cache_read_input_tokens", 0) or 0,
            getattr(u, "cache_creation_input_tokens", 0) or 0,
        )
        cost = PER_TOKEN * (tokens[0] * p.input + tokens[1] * p.output
                            + tokens[2] * p.cache_read + tokens[3] * p.cache_write)
        return cost, tokens

    def record(self, principal: Principal, response: Any) -> float:
        """Charge one API response to a principal. Returns the cost in USD."""
        usage = response.usage
        # With server-side fallbacks, iterations holds every attempt; top-level
        # usage only covers the attempt that produced the message.
        attempts = getattr(usage, "iterations", None) or [usage]
        now = time.time()
        total = 0.0
        for a in attempts:
            model = getattr(a, "model", None) or response.model
            cost, (i, o, cr, cw) = self._cost(model, a)
            self.db.execute("INSERT INTO spend VALUES (?,?,?,?,?,?,?,?)",
                            (now, principal.id, model, i, o, cr, cw, cost))
            total += cost
        self.db.commit()
        return total

    # -- queries ---------------------------------------------------------------

    def total(self) -> float:
        return self.db.execute("SELECT COALESCE(SUM(cost),0) FROM spend").fetchone()[0]

    def user_spend(self, user_id: str, since: float = 0.0) -> float:
        return self.db.execute(
            "SELECT COALESCE(SUM(cost),0) FROM spend WHERE principal=? AND ts>?",
            (user_id, since)).fetchone()[0]

    def exhausted(self) -> bool:
        return self.total() >= self.cap

    def user_limit(self) -> float:
        return self.cap * self.cfg.user_block_fraction

    def over_user_limit(self, p: Principal) -> bool:
        if self.perms.is_privileged(p):
            return False
        return self.user_spend(p.id, self.perms.spend_reset_at(p.id)) > self.user_limit()

    def pending_events(self) -> list[BudgetEvent]:
        """Threshold events not yet announced. Each fires once per arming."""
        events = []
        spent, cap = self.total(), self.cap
        if spent >= cap * self.cfg.warn_fraction and not self._get("warned"):
            self._set("warned", "1")
            if spent < cap:
                events.append(BudgetEvent.WARNING)
        if spent >= cap and not self._get("exhausted_notified"):
            self._set("exhausted_notified", "1")
            events.append(BudgetEvent.EXHAUSTED)
        return events

    def status(self) -> dict:
        return {
            "spent_usd": round(self.total(), 4),
            "cap_usd": self.cap,
            "remaining_usd": round(max(self.cap - self.total(), 0), 4),
            "per_user_limit_usd": round(self.user_limit(), 4),
            "ai_enabled": not self.exhausted(),
        }
