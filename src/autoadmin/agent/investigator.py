"""Read-only sub-agent that investigates a failure and returns a short report.

It runs in its own throwaway conversation so log reading never reaches the
main agent's context. It can only read; the orchestrating agent decides what
to do with the report.
"""

from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

import anthropic

from ..auth import Principal
from ..config import JailError
from ..services import Services

log = logging.getLogger(__name__)

MAX_LINES = 200
MAX_LINE_CHARS = 400
MAX_GREP = 50
SECRET_KEYS = re.compile(r"^(rcon\.password)=.*$", re.M)

SYSTEM = """\
You investigate why a Minecraft server failed to install, start, or keep running. \
You can only read files; you never change anything.

Work efficiently: start from the excerpt you're given, then read or grep only what you \
need to confirm a cause. Typical causes: a mod built for another Minecraft or loader \
version, a missing dependency, two incompatible mods, a bad config value, wrong Java \
version, port already in use, out of memory.

When done, call submit_report exactly once. Give cause_key as a short stable id so the \
same cause gets the same key next time (e.g. mod-crash:<modid>, missing-dependency:<modid>, \
oom, port-in-use). Set understood=true only when the evidence \
directly shows the cause. Quote short log lines as evidence. Suggested actions should be \
concrete and reversible where possible (e.g. "disable mods/foo-1.2.jar").
"""

TOOLS = [
    {
        "name": "read_file",
        "description": "Read lines from a log, crash report, or config file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Runtime-relative path, e.g. worlds/<name>/logs/latest.log"},
                "start_line": {"type": "integer", "description": "1-based first line"},
                "max_lines": {"type": "integer", "description": f"At most {MAX_LINES}"},
            },
            "required": ["path", "start_line", "max_lines"],
        },
    },
    {
        "name": "grep",
        "description": "Search a file with a regular expression. Returns matching lines with numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
            },
            "required": ["path", "pattern"],
        },
    },
    {
        "name": "list_mods",
        "description": "List jar files in the server's mods folder.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "read_mod_metadata",
        "description": "Read a mod jar's metadata (fabric.mod.json or neoforge.mods.toml).",
        "input_schema": {
            "type": "object",
            "properties": {"jar": {"type": "string", "description": "File name in mods/"}},
            "required": ["jar"],
        },
    },
    {
        "name": "submit_report",
        "description": "Submit the final investigation report. Ends the investigation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "understood": {"type": "boolean"},
                "cause": {"type": "string", "description": "One or two sentences."},
                "cause_key": {"type": "string",
                              "description": "Short stable id for this cause, kebab-case, e.g. "
                                             "missing-dependency:cloth-config, oom, "
                                             "mod-crash:create, port-in-use, unknown"},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "evidence": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
                "suggested_actions": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            },
            "required": ["understood", "cause", "cause_key", "confidence", "evidence",
                         "suggested_actions"],
        },
    },
]


def unknown_report(incident_id: str, why: str) -> str:
    return json.dumps({"incident_id": incident_id, "understood": False, "cause": why,
                       "cause_key": "unknown", "confidence": "low", "evidence": [],
                       "suggested_actions": []})


class Investigator:
    def __init__(self, services: Services, client: Any | None = None):
        self.services = services
        self.client = client or anthropic.AsyncAnthropic()

    @property
    def server_dir(self) -> Path:
        return self.services.server.dir

    # -- read-only file access ---------------------------------------------------

    def _readable(self, rel: str, extra: list[str]) -> Path:
        jail = self.services.jail
        p = jail.path(rel)
        allowed_roots = [self.server_dir / d for d in ("logs", "crash-reports", "config")]
        allowed_roots.append(jail.path("logs"))
        allowed_files = {jail.path(x) for x in extra}
        allowed_files |= {self.server_dir / "console.log", self.server_dir / "server.properties"}
        if p in allowed_files or any(r in p.parents for r in allowed_roots):
            if p.is_file():
                return p
            raise FileNotFoundError(rel)
        raise JailError(f"{rel} is not readable by the investigator")

    @staticmethod
    def _lines(p: Path) -> list[str]:
        text = SECRET_KEYS.sub(r"\1=<redacted>", p.read_text(errors="replace"))
        return text.splitlines()

    def _tool(self, name: str, args: dict, extra: list[str]) -> str:
        if name == "read_file":
            lines = self._lines(self._readable(args["path"], extra))
            start = max(1, int(args.get("start_line", 1)))
            n = min(MAX_LINES, max(1, int(args.get("max_lines", 100))))
            chunk = lines[start - 1:start - 1 + n]
            body = "\n".join(f"{start + i}: {l[:MAX_LINE_CHARS]}" for i, l in enumerate(chunk))
            return f"({len(lines)} lines total)\n{body}"
        if name == "grep":
            lines = self._lines(self._readable(args["path"], extra))
            rx = re.compile(args["pattern"])
            hits = [f"{i + 1}: {l[:MAX_LINE_CHARS]}" for i, l in enumerate(lines) if rx.search(l)]
            more = f"\n... {len(hits) - MAX_GREP} more" if len(hits) > MAX_GREP else ""
            return ("\n".join(hits[:MAX_GREP]) + more) or "no matches"
        if name == "list_mods":
            mods = sorted(p.name for p in (self.server_dir / "mods").glob("*.jar"))
            return "\n".join(mods) or "no mods installed"
        if name == "read_mod_metadata":
            jar = self.server_dir / "mods" / Path(args["jar"]).name
            with zipfile.ZipFile(jar) as z:
                for meta in ("fabric.mod.json", "META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                    if meta in z.namelist():
                        return f"{meta}:\n" + z.read(meta).decode(errors="replace")[:4000]
            return "no loader metadata found in jar"
        raise ValueError(f"unknown tool {name}")

    # -- loop -------------------------------------------------------------------------

    async def run(self, incident_id: str, question: str, principal: Principal) -> str:
        inc = self.services.incidents.get(incident_id)
        if inc is None:
            return json.dumps({"error": f"no incident {incident_id}"})
        if self.services.budget.exhausted():
            return unknown_report(incident_id, "Not investigated: the AI budget is used up.")
        cfg = self.services.config.investigator
        st = self.services.server.state()
        context = {
            "server": None if st is None else
                f"Minecraft {st.minecraft_version}, {st.loader} {st.loader_version or ''}, Java {st.java_major}",
            "incident": inc.summary,
            "readable_logs": inc.log_files,
        }
        messages: list = [{"role": "user", "content": (
            f"{json.dumps(context)}\n\nExcerpt:\n```\n{inc.excerpt}\n```\n\nQuestion: {question}")}]

        for step in range(cfg.max_steps):
            response = await self.client.messages.create(
                model=cfg.model,
                max_tokens=cfg.max_tokens,
                thinking={"type": "enabled", "budget_tokens": cfg.thinking_budget},
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            self.services.budget.record(principal, response)
            if response.stop_reason in ("refusal", "max_tokens"):
                break
            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                break
            results: list[dict] = []
            for call in calls:
                if call.name == "submit_report":
                    report = {k: call.input.get(k) for k in
                              ("understood", "cause", "cause_key", "confidence", "evidence",
                               "suggested_actions")}
                    report["incident_id"] = incident_id
                    return json.dumps(report)
                try:
                    out, err = self._tool(call.name, call.input or {}, inc.log_files), False
                except Exception as e:
                    out, err = f"{type(e).__name__}: {e}", True
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": out, "is_error": err})
            if step == cfg.max_steps - 2:
                results.append({"type": "text",
                                "text": "Step limit reached. Call submit_report now with what you have."})
            messages.append({"role": "user", "content": results})

        return unknown_report(incident_id, "Investigation ended without a report.")
