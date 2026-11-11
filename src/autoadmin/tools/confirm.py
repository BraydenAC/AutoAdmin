"""Two-step confirmation for destructive actions.

The first call registers the request and tells the model to ask. Only a repeat
call with the same arguments, from the same person, in that person's very next
message in the conversation, goes through. The model can't confirm on anyone's
behalf within one turn, and a request that wasn't confirmed right away expires:
asking again later starts over with a new question.
"""

from __future__ import annotations

import json

from .registry import ToolContext


def note_turn(conv_state: dict, principal_id: str, turn: int) -> None:
    """Record that `principal_id` sent the message running as `turn`. The router calls
    this for every human message, so "their previous message" is always known."""
    turns: dict = conv_state.setdefault("turns", {})
    _, current = turns.get(principal_id, (None, None))
    if current != turn:
        turns[principal_id] = (current, turn)


def previous_turn(ctx: ToolContext) -> int | None:
    """The turn of the requester's message before the current one in this conversation."""
    entry = ctx.conv_state.get("turns", {}).get(ctx.principal.id)
    return entry[0] if entry and entry[1] == ctx.turn else None


def confirmed(ctx: ToolContext, operation: str, args: dict, summary: str) -> str | None:
    """Return None when confirmed (go ahead), or the text to return to the model."""
    # Keyed per person, so someone else repeating the request can't hijack or reset it.
    prefix = f"{ctx.principal.id}:"
    key = f"{prefix}{operation}:{json.dumps(args, sort_keys=True)}"
    pending: dict = ctx.conv_state.setdefault("pending_confirmations", {})
    prev = previous_turn(ctx)
    asked = pending.pop(key, None)
    if asked is not None and prev is not None and asked == prev:
        return None
    # This person's requests from before their previous message were never confirmed.
    for k, t in list(pending.items()):
        if k.startswith(prefix) and t not in (prev, ctx.turn):
            del pending[k]
    pending[key] = ctx.turn
    return (f"CONFIRMATION NEEDED: {summary}. Ask {ctx.principal.name} to reply yes to confirm, "
            "then call this again with the same arguments in response to their reply. Do not "
            "repeat the call in this turn.")
