"""Tools for permissions, admins, the budget, and resource usage."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..auth import GRANTABLE, Perm, PermissionError_
from ..server import resources
from .registry import Registry, ToolContext, ToolError

OVERVIEW_VIEWS = ["me", "permissions", "budget", "resources"]

# set_user_access action -> permission the requester needs.
ACCESS_ACTIONS: dict[str, Perm] = {
    "grant_permission": Perm.PERMS_GRANT,
    "revoke_permission": Perm.PERMS_GRANT,
    "block": Perm.PERMS_GRANT,
    "unblock": Perm.PERMS_GRANT,
    "grant_admin": Perm.ADMINS_MANAGE,
    "revoke_admin": Perm.ADMINS_MANAGE,
    "grant_budget_manager": Perm.ADMINS_MANAGE,
    "revoke_budget_manager": Perm.ADMINS_MANAGE,
}
NO_PERMISSION = "none"


def _access_permission(args: dict[str, Any]) -> Perm:
    action = args.get("action")
    if action not in ACCESS_ACTIONS:
        raise ToolError(f"unknown action '{action}'")
    return ACCESS_ACTIONS[action]


def _target_name(ctx: ToolContext, user_id: str, *, must_mention: bool) -> str:
    if user_id in ctx.mentions:
        return ctx.mentions[user_id]
    if must_mention:
        raise ToolError("that user must be @mentioned in the requester's current message")
    if ctx.services.perms.knows(user_id):
        return ctx.services.perms.name_of(user_id)
    raise ToolError("unknown user; ask the requester to @mention them")


def register(r: Registry) -> None:

    @r.tool("access_overview",
            "Read-only overview. view=me: requester's role and permissions. permissions: owner, "
            "admins, budget managers, users with extra permissions or blocks. budget: AI spend, "
            "cap, per-user limit. resources: host CPU/RAM/disk, the Minecraft process's live "
            "CPU and memory, and disk used by worlds, Java, caches, and logs.",
            permission=Perm.CHAT,
            properties={"view": {"type": "string", "enum": OVERVIEW_VIEWS}})
    async def access_overview(ctx: ToolContext, view: str) -> str:
        s = ctx.services
        if view == "me":
            return json.dumps({"name": ctx.principal.name, "role": s.perms.role(ctx.principal),
                               "permissions": sorted(s.perms.effective(ctx.principal))})
        if view == "permissions":
            return json.dumps(s.perms.summary())
        if view == "budget":
            return json.dumps(s.budget.status())
        if view == "resources":
            st = s.server.state()
            proc = await resources.process(s.server.unit, st.memory_mb if st else None)
            disk = await asyncio.to_thread(resources.storage, s.jail.root)
            return json.dumps({"host": resources.host(), "server_process": proc, **disk})
        raise ToolError(f"unknown view '{view}'")

    @r.tool("set_user_access",
            "Change a user's access. Actions: grant_permission / revoke_permission (regular users; "
            "set `permission`), block / unblock (unblock also resets their AI spend), "
            "grant_admin / revoke_admin and grant_budget_manager / revoke_budget_manager (owner "
            "only). grant_admin needs the user @mentioned in the current message. Use "
            f"permission='{NO_PERMISSION}' for actions that don't take one.",
            permission=_access_permission,
            properties={
                "user_id": {"type": "string", "description": "Discord user ID from the message header."},
                "action": {"type": "string", "enum": list(ACCESS_ACTIONS)},
                "permission": {"type": "string",
                               "enum": [NO_PERMISSION, *sorted(str(p) for p in GRANTABLE)]},
            })
    async def set_user_access(ctx: ToolContext, user_id: str, action: str, permission: str) -> str:
        perms = ctx.services.perms
        name = _target_name(ctx, user_id, must_mention=action == "grant_admin")
        needs_perm = action in ("grant_permission", "revoke_permission")
        if needs_perm and permission == NO_PERMISSION:
            raise ToolError(f"{action} needs a permission")
        try:
            match action:
                case "grant_permission":
                    perms.grant(user_id, name, Perm(permission))
                    return f"Granted {permission} to {name}."
                case "revoke_permission":
                    perms.revoke(user_id, Perm(permission))
                    return f"Revoked {permission} from {name}."
                case "block":
                    perms.block(user_id, name)
                    return f"{name} is blocked."
                case "unblock":
                    perms.unblock(user_id)
                    return f"{name} is unblocked."
                case "grant_admin":
                    perms.grant_admin(user_id, name)
                    return f"{name} is now an admin."
                case "revoke_admin":
                    perms.revoke_admin(user_id)
                    return f"{name} is no longer an admin."
                case "grant_budget_manager" | "revoke_budget_manager":
                    enabled = action == "grant_budget_manager"
                    perms.set_budget_manager(user_id, enabled)
                    return f"{name} {'can' if enabled else 'can no longer'} manage the budget."
        except PermissionError_ as e:
            raise ToolError(str(e))
        raise ToolError(f"unknown action '{action}'")

    @r.tool("set_budget_cap", "Set the total AI budget cap in USD.",
            permission=Perm.BUDGET_MANAGE,
            properties={"usd": {"type": "number", "description": "New cap in USD."}})
    async def set_budget_cap(ctx: ToolContext, usd: float) -> str:
        try:
            ctx.services.budget.set_cap(usd)
        except ValueError as e:
            raise ToolError(str(e))
        return json.dumps(ctx.services.budget.status())
