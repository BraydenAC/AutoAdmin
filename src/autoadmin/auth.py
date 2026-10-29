"""Principals, roles, and the deterministic permission store.

Identity is always bound by the harness (the Discord message author, or the
system principal for supervisor turns). The model never supplies it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class Perm(StrEnum):
    CHAT = "chat"
    SERVER_CONTROL = "server.control"
    SERVER_CONSOLE = "server.console"
    PLAYERS_MANAGE = "players.manage"
    MODS_MANAGE = "mods.manage"
    CONFIG_EDIT = "config.edit"
    WORLDS_MANAGE = "worlds.manage"
    BACKUPS_CREATE = "backups.create"
    BACKUPS_RESTORE = "backups.restore"
    WORLDS_DELETE = "worlds.delete"
    PERMS_GRANT = "perms.grant"
    BUDGET_MANAGE = "budget.manage"
    ADMINS_MANAGE = "admins.manage"
    # Only ever held by the system principal.
    CRASH_RECOVERY = "system.crash_recovery"


# What admins may hand out to regular users.
GRANTABLE: frozenset[Perm] = frozenset({
    Perm.CHAT, Perm.SERVER_CONTROL, Perm.SERVER_CONSOLE, Perm.PLAYERS_MANAGE,
    Perm.MODS_MANAGE, Perm.CONFIG_EDIT, Perm.WORLDS_MANAGE, Perm.BACKUPS_CREATE,
    Perm.BACKUPS_RESTORE, Perm.WORLDS_DELETE,
})

ADMIN_PERMS: frozenset[Perm] = GRANTABLE | {Perm.PERMS_GRANT}
OWNER_PERMS: frozenset[Perm] = ADMIN_PERMS | {Perm.BUDGET_MANAGE, Perm.ADMINS_MANAGE}
SYSTEM_PERMS: frozenset[Perm] = frozenset({
    Perm.CHAT, Perm.SERVER_CONTROL, Perm.BACKUPS_CREATE, Perm.CRASH_RECOVERY,
})
DEFAULT_USER_PERMS: frozenset[Perm] = frozenset({Perm.CHAT})


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    USER = "user"
    SYSTEM = "system"


@dataclass(frozen=True)
class Principal:
    id: str
    name: str
    is_system: bool = False

    def __str__(self) -> str:
        return "system" if self.is_system else f"{self.name}({self.id})"


SYSTEM = Principal(id="system", name="AutoAdmin", is_system=True)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


class PermissionError_(Exception):
    """Raised for invalid permission-management requests (not plain denials)."""


class PermissionStore:
    def __init__(self, path: Path):
        self.path = path
        self._data: dict = {"owner": None, "admins": [], "budget_managers": [], "users": {}}
        if path.exists():
            self._data.update(json.loads(path.read_text()))

    # -- persistence -------------------------------------------------------

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def _user(self, user_id: str, name: str | None = None) -> dict:
        u = self._data["users"].setdefault(
            user_id, {"name": name or user_id, "perms": [], "blocked": False,
                      "spend_reset_at": 0.0})
        if name:
            u["name"] = name
        return u

    # -- identity ----------------------------------------------------------

    @property
    def owner_id(self) -> str | None:
        return self._data["owner"]

    def set_owner(self, user_id: str, name: str) -> None:
        """Bootstrap: the guild owner. Called by the adapter on startup."""
        if self._data["owner"] != user_id:
            self._data["owner"] = user_id
        self._user(user_id, name)
        self._save()

    def remember(self, p: Principal) -> None:
        if p.is_system:
            return
        known = self._data["users"].get(p.id)
        if known is None or known["name"] != p.name:
            self._user(p.id, p.name)
            self._save()

    def name_of(self, user_id: str) -> str:
        u = self._data["users"].get(user_id)
        return u["name"] if u else user_id

    def knows(self, user_id: str) -> bool:
        return user_id in self._data["users"]

    def role(self, p: Principal) -> Role:
        if p.is_system:
            return Role.SYSTEM
        if p.id == self._data["owner"]:
            return Role.OWNER
        if p.id in self._data["admins"]:
            return Role.ADMIN
        return Role.USER

    def is_privileged(self, p: Principal) -> bool:
        return self.role(p) in (Role.OWNER, Role.ADMIN, Role.SYSTEM)

    @property
    def admin_ids(self) -> list[str]:
        return list(self._data["admins"])

    # -- checks ------------------------------------------------------------

    def effective(self, p: Principal) -> frozenset[Perm]:
        role = self.role(p)
        if role is Role.SYSTEM:
            return SYSTEM_PERMS
        if role is Role.OWNER:
            return OWNER_PERMS
        if role is Role.ADMIN:
            extra = {Perm.BUDGET_MANAGE} if p.id in self._data["budget_managers"] else set()
            return ADMIN_PERMS | extra
        u = self._data["users"].get(p.id)
        if u and u["blocked"]:
            return frozenset()
        granted = {Perm(x) for x in u["perms"]} if u else set()
        return DEFAULT_USER_PERMS | granted

    def check(self, p: Principal, perm: Perm) -> Decision:
        if self.is_blocked(p.id) and not self.is_privileged(p):
            return Decision(False, "you are blocked; ask an admin to unblock you")
        if perm in self.effective(p):
            return Decision(True)
        return Decision(False, f"missing permission '{perm}'")

    # -- management (callers must already have passed the permission gate) --

    def grant_admin(self, user_id: str, name: str) -> None:
        if user_id == self._data["owner"]:
            raise PermissionError_("the owner is already above admin")
        self._user(user_id, name)
        if user_id not in self._data["admins"]:
            self._data["admins"].append(user_id)
        self._data["users"][user_id]["blocked"] = False
        self._save()

    def revoke_admin(self, user_id: str) -> None:
        if user_id not in self._data["admins"]:
            raise PermissionError_("that user is not an admin")
        self._data["admins"].remove(user_id)
        if user_id in self._data["budget_managers"]:
            self._data["budget_managers"].remove(user_id)
        self._save()

    def set_budget_manager(self, user_id: str, enabled: bool) -> None:
        if enabled:
            if user_id not in self._data["admins"]:
                raise PermissionError_("budget.manage can only be granted to admins")
            if user_id not in self._data["budget_managers"]:
                self._data["budget_managers"].append(user_id)
        elif user_id in self._data["budget_managers"]:
            self._data["budget_managers"].remove(user_id)
        self._save()

    def grant(self, user_id: str, name: str, perm: Perm) -> None:
        if perm not in GRANTABLE:
            raise PermissionError_(f"'{perm}' cannot be granted to regular users")
        u = self._user(user_id, name)
        if perm not in u["perms"]:
            u["perms"].append(str(perm))
            u["perms"].sort()
        self._save()

    def revoke(self, user_id: str, perm: Perm) -> None:
        u = self._data["users"].get(user_id)
        if not u or str(perm) not in u["perms"]:
            raise PermissionError_(f"user does not have '{perm}'")
        u["perms"].remove(str(perm))
        self._save()

    def is_blocked(self, user_id: str) -> bool:
        u = self._data["users"].get(user_id)
        return bool(u and u["blocked"])

    def block(self, user_id: str, name: str | None = None) -> None:
        if user_id == self._data["owner"] or user_id in self._data["admins"]:
            raise PermissionError_("owner and admins cannot be blocked")
        self._user(user_id, name)["blocked"] = True
        self._save()

    def unblock(self, user_id: str) -> None:
        """Unblocking also resets the user's spend counter."""
        u = self._data["users"].get(user_id)
        if not u or not u["blocked"]:
            raise PermissionError_("that user is not blocked")
        u["blocked"] = False
        u["spend_reset_at"] = time.time()
        self._save()

    def spend_reset_at(self, user_id: str) -> float:
        u = self._data["users"].get(user_id)
        return u["spend_reset_at"] if u else 0.0

    def summary(self) -> dict:
        """Readable snapshot for the model / status tools."""
        users = self._data["users"]
        return {
            "owner": self.name_of(self._data["owner"]) if self._data["owner"] else None,
            "admins": [self.name_of(a) for a in self._data["admins"]],
            "budget_managers": [self.name_of(a) for a in self._data["budget_managers"]],
            "users": {
                u["name"]: {"perms": u["perms"], "blocked": u["blocked"]}
                for uid, u in users.items()
                if uid != self._data["owner"] and uid not in self._data["admins"]
                and (u["perms"] or u["blocked"])
            },
        }
