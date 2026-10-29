"""Typed tools with a deterministic permission gate and audit trail.

Every call is executed as the principal the harness bound to the turn; the
model has no way to name a different identity.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..auth import Perm, Principal
from ..services import Services

log = logging.getLogger(__name__)

# Hard cap on what any tool can put into the model's context (~2k tokens).
MAX_RESULT_CHARS = 8000
# API limit on tools marked strict in one request.
MAX_STRICT_TOOLS = 20


class ToolError(Exception):
    """A tool failed in a way the model should see and can react to."""


class ToolDenied(ToolError):
    """A dynamic permission check inside a tool refused the call."""


@dataclass
class ToolContext:
    principal: Principal
    services: Services
    # Users @mentioned in the message that triggered this turn: id -> name.
    # Tools that target a user may require the target to appear here, so the
    # model cannot invent an ID.
    mentions: dict[str, str] = field(default_factory=dict)
    # Posts a progress line straight to the channel, without a model call.
    progress: Callable[[str], Awaitable[None]] | None = None
    # Monotonic id of the human message that triggered this turn.
    turn: int = 0
    # Per-conversation scratch state owned by tools (e.g. pending EULA prompt).
    conv_state: dict = field(default_factory=dict)

    async def say(self, text: str) -> None:
        if self.progress:
            await self.progress(text)

    def check(self, perm: Perm) -> None:
        d = self.services.perms.check(self.principal, perm)
        if not d.allowed:
            raise ToolDenied(d.reason)


Handler = Callable[..., Awaitable[str]]
PermSpec = Perm | Callable[[dict[str, Any]], Perm] | None


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]
    permission: PermSpec
    handler: Handler

    def required_perm(self, args: dict[str, Any]) -> Perm | None:
        if callable(self.permission) and not isinstance(self.permission, Perm):
            return self.permission(args)
        return self.permission

    def api_schema(self) -> dict:
        # Strict validation only matters for tools that take arguments, and the
        # API caps how many tools may be strict.
        return {
            "name": self.name,
            "description": self.description,
            **({"strict": True} if self.properties else {}),
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": sorted(self.properties),
                "additionalProperties": False,
            },
        }


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def tool(self, name: str, description: str, *, permission: PermSpec,
             properties: dict[str, dict] | None = None) -> Callable[[Handler], Handler]:
        def register(fn: Handler) -> Handler:
            if name in self._tools:
                raise ValueError(f"duplicate tool {name}")
            self._tools[name] = Tool(name, description, properties or {}, permission, fn)
            return fn
        return register

    def api_tools(self) -> list[dict]:
        # Sorted so the tools prefix is byte-stable for prompt caching.
        tools = [self._tools[n].api_schema() for n in sorted(self._tools)]
        strict = sum(1 for t in tools if t.get("strict"))
        if strict > MAX_STRICT_TOOLS:
            raise ValueError(f"{strict} strict tools; the API allows {MAX_STRICT_TOOLS}")
        return tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    async def execute(self, ctx: ToolContext, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool call. Returns (result_text, is_error)."""
        audit = ctx.services.audit
        who = str(ctx.principal)
        tool = self._tools.get(name)
        if tool is None:
            audit.record(principal=who, tool=name, args=args, allowed=False, result="unknown tool")
            return f"Unknown tool '{name}'.", True

        missing = set(tool.properties) - set(args)
        if missing:
            return f"Missing arguments: {', '.join(sorted(missing))}", True

        try:
            perm = tool.required_perm(args)
        except ToolError as e:
            audit.record(principal=who, tool=name, args=args, allowed=False, result=str(e))
            return str(e), True
        if perm is not None:
            decision = ctx.services.perms.check(ctx.principal, perm)
            if not decision.allowed:
                msg = f"DENIED for {ctx.principal.name}: {decision.reason}"
                audit.record(principal=who, tool=name, args=args, allowed=False, result=msg)
                return msg, True

        try:
            result = await tool.handler(ctx, **args)
        except ToolDenied as e:
            msg = f"DENIED for {ctx.principal.name}: {e}"
            audit.record(principal=who, tool=name, args=args, allowed=False, result=msg)
            return msg, True
        except ToolError as e:
            audit.record(principal=who, tool=name, args=args, allowed=True, result=str(e), error=True)
            return f"Error: {e}", True
        except Exception as e:
            log.exception("tool %s crashed", name)
            audit.record(principal=who, tool=name, args=args, allowed=True,
                         result=f"internal error: {e!r}", error=True)
            return f"Internal error in {name}: {type(e).__name__}: {e}", True

        audit.record(principal=who, tool=name, args=args, allowed=True, result=result)
        if len(result) > MAX_RESULT_CHARS:
            result = result[:MAX_RESULT_CHARS] + "\n... (output truncated)"
        return result, False
