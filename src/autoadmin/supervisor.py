"""Watches the Minecraft server and recovers from crashes.

Code owns the policy: when to restart, when to stop and ask admins. Claude is
only used to diagnose (investigator) and to apply a reversible fix.

Per crash:
  1. Pre-filtered incident -> investigator report (understood?, cause_key, ...).
  2. Escalate instead of restarting if the previous crash wasn't understood, the
     same cause came back, or there were too many crashes in the window.
  3. Otherwise, if understood, a short system-principal agent turn applies a fix.
  4. Always restart. A failed restart is the next crash in the chain.
Crash history resets after a stretch of stable uptime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .agent.investigator import unknown_report
from .auth import SYSTEM
from .server.incidents import Incident
from .server.manager import ServerError, StartOutcome
from .services import Services
from .sessions import Router, Send

log = logging.getLogger(__name__)


@dataclass
class CrashEntry:
    ts: float
    incident_id: str
    understood: bool
    cause_key: str
    cause: str


@dataclass
class SupervisorState:
    chain: list[CrashEntry] = field(default_factory=list)
    escalated: bool = False


class Supervisor:
    def __init__(self, services: Services, router: Router, send: Send,
                 investigator: Any | None):
        self.s = services
        self.cfg = services.config.supervisor
        self.router = router
        self.send = send
        self.investigator = investigator
        self._path = services.jail.path("supervisor.json")
        self.state = self._load()

    # -- persistence -------------------------------------------------------------------

    def _load(self) -> SupervisorState:
        try:
            raw = json.loads(self._path.read_text())
        except FileNotFoundError:
            return SupervisorState()
        return SupervisorState([CrashEntry(**e) for e in raw.get("chain", [])],
                               raw.get("escalated", False))

    def _save(self) -> None:
        self._path.write_text(json.dumps(asdict(self.state), indent=2))

    def _admin_ids(self) -> list[str]:
        perms = self.s.perms
        return [i for i in [perms.owner_id, *perms.admin_ids] if i]

    async def _say(self, text: str, *, ping_admins: bool = False, context: str | None = None) -> None:
        await self.router.post_system(self.send, text,
                                      ping_ids=self._admin_ids() if ping_admins else None,
                                      context=context)

    # -- main loop ----------------------------------------------------------------------

    async def run(self) -> None:
        if self.cfg.autostart:
            await self.autostart()
        while True:
            await asyncio.sleep(self.cfg.poll_interval_s)
            try:
                await self.tick()
            except Exception:
                log.exception("supervisor tick failed")

    async def autostart(self) -> None:
        server = self.s.server
        if (server.state() is None or server.desired != "running" or self.state.escalated
                or await server.running()):
            return
        await self._say("Starting the Minecraft server back up (it was running before I restarted).")
        await self._start_and_handle()

    async def tick(self) -> None:
        server = self.s.server
        if server.state() is None or server.busy:
            return
        unit = await server.unit.state()
        if unit.running:
            if self.state.escalated:
                # A human started it again: resume normal supervision.
                self.state = SupervisorState()
                self._save()
            elif self.state.chain:
                acct = await server.unit.accounting()
                if acct.get("uptime_s", 0) >= self.cfg.stable_uptime_s:
                    self.state.chain.clear()
                    self._save()
            return
        if server.desired != "running" or self.state.escalated:
            return
        if unit.result == "success" and unit.exit_status in (0, None):
            server.set_desired("stopped")
            await self._say("The Minecraft server was stopped from inside the game; "
                            "I won't restart it.")
            return
        await self.handle_crash(server.crash_incident(unit))

    # -- crash handling --------------------------------------------------------------------

    async def _investigate(self, inc: Incident) -> dict:
        if self.investigator is None or self.s.budget.exhausted():
            return json.loads(unknown_report(inc.id, "Not investigated (no AI available)."))
        try:
            return json.loads(await self.investigator.run(
                inc.id, "Why did the server crash, and what reversible fix would help?", SYSTEM))
        except Exception as e:
            log.exception("investigation failed")
            return json.loads(unknown_report(inc.id, f"Investigation failed: {e}"))

    def _escalation_reason(self, report: dict) -> str | None:
        chain = self.state.chain
        prev = chain[-2] if len(chain) >= 2 else None
        if prev and not prev.understood:
            return "it crashed again, and I couldn't work out the previous crash"
        if prev and report.get("understood") and report.get("cause_key") == prev.cause_key:
            return f"the same problem came back after my fix ({prev.cause_key})"
        if len(chain) > self.cfg.max_crashes:
            return (f"it has crashed {len(chain)} times in the last "
                    f"{self.cfg.window_s // 60} minutes")
        return None

    async def handle_crash(self, inc: Incident) -> None:
        """Investigate, maybe fix, restart; repeat if the restart fails."""
        while True:
            report = await self._investigate(inc)
            now = time.time()
            self.state.chain = [e for e in self.state.chain if now - e.ts <= self.cfg.window_s]
            self.state.chain.append(CrashEntry(
                now, inc.id, bool(report.get("understood")),
                str(report.get("cause_key") or "unknown"), str(report.get("cause") or "")))
            self._save()

            context = (f"{inc.brief()}\n\nInvestigator report:\n{json.dumps(report)}")
            if reason := self._escalation_reason(report):
                await self._escalate(reason, report, context)
                return

            if report.get("understood") and not self.s.budget.exhausted():
                await self.router.system_turn(self.send, (
                    f"{context}\n\nThe server crashed. If the report points to a reversible fix "
                    "(disable a mod, change a setting such as memory-mb), apply it now with your "
                    "tools. Do not start or stop the server; I'll restart it right after this. "
                    "Then reply in one short line: what crashed and what you changed, if anything."))
            else:
                await self._say("The Minecraft server crashed and I couldn't tell why, so I'm "
                                "restarting it as-is. If it crashes again I'll ask the admins.",
                                context=context)

            next_inc = await self._start_and_handle(report_restart=True)
            if next_inc is None:
                return
            inc = next_inc

    async def _start_and_handle(self, report_restart: bool = False) -> Incident | None:
        """Start the server. Returns the incident if the start failed, after handling
        it inline when called from autostart."""
        try:
            outcome, message = await self.s.server.start()
        except ServerError as e:
            await self._escalate(f"I couldn't start it: {e}", {}, str(e))
            return None
        if outcome in (StartOutcome.READY, StartOutcome.ALREADY_RUNNING):
            if report_restart:
                await self._say(f"Restarted. {message}")
            return None
        if outcome is StartOutcome.EULA_REQUIRED:
            await self._escalate("it needs the EULA accepted before it can start", {}, message)
            return None
        inc_id = message.split("incident_id: ")[1].split()[0] if "incident_id: " in message else None
        inc = self.s.incidents.get(inc_id) if inc_id else None
        if inc is None:
            await self._escalate(f"it failed to start: {message}", {}, message)
            return None
        if report_restart:
            return inc
        await self.handle_crash(inc)
        return None

    async def _escalate(self, reason: str, report: dict, context: str) -> None:
        self.state.escalated = True
        self._save()
        cause, inc = report.get("cause"), report.get("incident_id")
        diagnosis = (f" Last diagnosis: {cause}" + (f" (incident {inc})." if inc else ".")
                     if cause else "")
        await self._say(
            f"The Minecraft server is down and I've stopped restarting it: {reason}.{diagnosis} "
            "Reply here to tell me what to do; I'll resume normal supervision once the "
            "server is started again.",
            ping_admins=True, context=context)
