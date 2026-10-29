"""The Claude tool-use loop.

History is append-only: assistant turns are stored exactly as returned, so the
prompt cache and preserved thinking both stay valid.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anthropic

from ..auth import Principal
from ..services import Services
from ..tools import Registry, ToolContext
from .prompt import SYSTEM_PROMPT

log = logging.getLogger(__name__)

Emit = Callable[[str], Awaitable[None]]

FALLBACK_BETA = "server-side-fallback-2026-07-01"

BLOCKED_NOTICE = ("You've used your share of the AI budget, so I've paused your access. "
                  "An admin can unblock you.")
BUDGET_NOTICE = "My AI budget is used up, so I can only do `status`, `start`, and `stop` for now."


class Agent:
    def __init__(self, services: Services, registry: Registry,
                 client: Any | None = None):
        self.services = services
        self.registry = registry
        self.client = client or anthropic.AsyncAnthropic()
        self._tools = registry.api_tools()

    async def _call(self, messages: list) -> Any:
        cfg = self.services.config.agent
        return await self.client.beta.messages.create(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            # Explicit breakpoint after tools+system so every conversation shares
            # that prefix; top-level cache_control caches the growing history.
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            tools=self._tools,
            messages=messages,
            output_config={"effort": cfg.effort},
            cache_control={"type": "ephemeral"},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )

    async def run(self, messages: list, principal: Principal,
                  mentions: dict[str, str], emit: Emit, *, turn: int = 0,
                  conv_state: dict | None = None) -> None:
        """Run one turn. `messages` must already end with the new user message;
        it is extended in place with the assistant/tool turns."""
        budget, perms = self.services.budget, self.services.perms
        ctx = ToolContext(principal=principal, services=self.services, mentions=mentions,
                          progress=emit, turn=turn,
                          conv_state=conv_state if conv_state is not None else {})

        for _ in range(self.services.config.agent.max_steps):
            if budget.exhausted():
                await emit(BUDGET_NOTICE)
                return

            try:
                response = await self._call(messages)
            except anthropic.RateLimitError:
                await emit("I'm being rate limited right now. Try again in a minute.")
                return
            except anthropic.APIStatusError as e:
                log.error("API error %s: %s", e.status_code, e.message)
                await emit("Something went wrong on my end talking to the AI service.")
                return
            except anthropic.APIConnectionError:
                await emit("I can't reach the AI service right now.")
                return

            budget.record(principal, response)
            texts = [b.text for b in response.content
                     if b.type == "text" and b.text.strip()]

            if response.stop_reason == "refusal":
                # Not appended: a refused turn shouldn't shape later ones.
                await emit("I can't help with that one.")
                return
            if response.stop_reason == "max_tokens":
                # Not appended: it may hold a truncated tool call with no result.
                await emit("I got cut off mid-thought. Could you try a narrower request?")
                return

            messages.append({"role": "assistant", "content": response.content})
            calls = [b for b in response.content if b.type == "tool_use"]

            if budget.over_user_limit(principal):
                perms.block(principal.id, principal.name)
                if calls:
                    # Every tool_use needs a result, or the conversation can't continue.
                    messages.append({"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": c.id, "is_error": True,
                         "content": "Not run: requester was blocked for budget use."}
                        for c in calls]})
                await emit(BLOCKED_NOTICE)
                return

            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason != "tool_use" or not calls:
                if texts:
                    await emit("\n".join(texts))
                return

            if texts:
                await emit("\n".join(texts))
            results = []
            for call in calls:
                output, is_error = await self.registry.execute(ctx, call.name, call.input or {})
                results.append({"type": "tool_result", "tool_use_id": call.id,
                                "content": output, "is_error": is_error})
            messages.append({"role": "user", "content": results})

        await emit("That took more steps than I'm allowed in one go. Want me to keep going?")
