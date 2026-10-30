"""Two-step confirmation for destructive actions.

The first call registers the request and tells the model to ask. Only a repeat
call with the same arguments, from the same person, in a later human message,
goes through. The model can't confirm on anyone's behalf within one turn.
"""

from __future__ import annotations

import json

from .registry import ToolContext


def confirmed(ctx: ToolContext, operation: str, args: dict, summary: str) -> str | None:
    """Return None when confirmed (go ahead), or the text to return to the model."""
    # Keyed per person, so someone else repeating the request can't hijack or reset it.
    key = f"{ctx.principal.id}:{operation}:{json.dumps(args, sort_keys=True)}"
    pending: dict = ctx.conv_state.setdefault("pending_confirmations", {})
    turn = pending.get(key)
    if turn is not None and turn < ctx.turn:
        del pending[key]
        return None
    if turn is None:
        pending[key] = ctx.turn
    return (f"CONFIRMATION NEEDED: {summary}. Ask {ctx.principal.name} to reply yes to confirm, "
            "then call this again with the same arguments. Do not repeat the call in this turn.")
