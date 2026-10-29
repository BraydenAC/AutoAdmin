from __future__ import annotations

from typing import TYPE_CHECKING

from . import access, server
from .registry import Registry, Tool, ToolContext, ToolDenied, ToolError

if TYPE_CHECKING:
    from ..agent.investigator import Investigator


def build_registry(investigator: Investigator | None = None) -> Registry:
    r = Registry()
    access.register(r)
    server.register(r, investigator)
    return r


__all__ = ["Registry", "Tool", "ToolContext", "ToolDenied", "ToolError", "build_registry"]
