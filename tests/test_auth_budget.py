from __future__ import annotations

import json

import pytest

from autoadmin.auth import SYSTEM, Perm, PermissionError_, PermissionStore, Role
from autoadmin.budget import BudgetEvent

from .conftest import ADMIN, OWNER, USER, response


def test_roles_and_defaults(services):
    perms = services.perms
    assert perms.role(OWNER) is Role.OWNER
    assert perms.role(ADMIN) is Role.ADMIN
    assert perms.role(USER) is Role.USER
    assert perms.role(SYSTEM) is Role.SYSTEM
    assert perms.check(USER, Perm.CHAT).allowed
    assert not perms.check(USER, Perm.SERVER_CONTROL).allowed
    assert perms.check(ADMIN, Perm.WORLDS_DELETE).allowed
    assert not perms.check(ADMIN, Perm.ADMINS_MANAGE).allowed
    assert not perms.check(SYSTEM, Perm.WORLDS_DELETE).allowed
    assert not perms.check(SYSTEM, Perm.BACKUPS_RESTORE).allowed


def test_budget_manage_is_owner_granted_to_admins_only(services):
    perms = services.perms
    assert perms.check(OWNER, Perm.BUDGET_MANAGE).allowed
    assert not perms.check(ADMIN, Perm.BUDGET_MANAGE).allowed
    perms.set_budget_manager(ADMIN.id, True)
    assert perms.check(ADMIN, Perm.BUDGET_MANAGE).allowed
    with pytest.raises(PermissionError_):
        perms.set_budget_manager(USER.id, True)
    perms.revoke_admin(ADMIN.id)
    assert not perms.check(ADMIN, Perm.BUDGET_MANAGE).allowed


def test_grant_revoke_and_non_grantable(services):
    perms = services.perms
    perms.grant(USER.id, USER.name, Perm.SERVER_CONTROL)
    assert perms.check(USER, Perm.SERVER_CONTROL).allowed
    perms.revoke(USER.id, Perm.SERVER_CONTROL)
    assert not perms.check(USER, Perm.SERVER_CONTROL).allowed
    for p in (Perm.PERMS_GRANT, Perm.BUDGET_MANAGE, Perm.ADMINS_MANAGE, Perm.CRASH_RECOVERY):
        with pytest.raises(PermissionError_):
            perms.grant(USER.id, USER.name, p)


def test_block_removes_everything_and_persists(services, config):
    services.perms.grant(USER.id, USER.name, Perm.SERVER_CONTROL)
    services.perms.block(USER.id)
    assert not services.perms.check(USER, Perm.CHAT).allowed
    with pytest.raises(PermissionError_):
        services.perms.block(ADMIN.id)
    reloaded = PermissionStore(services.jail.path("permissions.json"))
    assert reloaded.is_blocked(USER.id)
    assert json.loads(services.jail.path("permissions.json").read_text())["owner"] == OWNER.id


def test_cost_and_threshold_events(services):
    b = services.budget
    assert b.record(USER, response(tokens=1_000_000)) == pytest.approx(1.0)
    assert b.pending_events() == []
    b.record(OWNER, response(tokens=7_000_000))
    assert b.pending_events() == [BudgetEvent.WARNING]
    assert b.pending_events() == []          # fires once
    b.record(OWNER, response(tokens=2_000_000))
    assert b.exhausted()
    assert b.pending_events() == [BudgetEvent.EXHAUSTED]
    b.set_cap(20.0)                           # raising re-arms both
    assert not b.exhausted()
    b.record(OWNER, response(tokens=7_000_000))
    assert b.pending_events() == [BudgetEvent.WARNING]


def test_unknown_model_priced_at_most_expensive(services):
    services.budget.pricing["cheap"] = type(services.budget.pricing["test-model"])(0, 0, 0, 0)
    r = response(tokens=1_000_000)
    r.model = "mystery"
    assert services.budget.record(OWNER, r) == pytest.approx(1.0)


def test_per_user_limit_and_unblock_reset(services):
    b, perms = services.budget, services.perms
    b.record(USER, response(tokens=600_000))      # $0.60 > 5% of $10
    assert b.over_user_limit(USER)
    b.record(ADMIN, response(tokens=5_000_000))
    assert not b.over_user_limit(ADMIN)           # admins exempt
    perms.block(USER.id)
    perms.unblock(USER.id)
    assert not b.over_user_limit(USER)            # counter reset
