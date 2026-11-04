from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from autoadmin.auth import Principal
from autoadmin.config import AgentConfig, BudgetConfig, Config, DiscordConfig, Price
from autoadmin.services import Services

OWNER = Principal("u-owner", "Owner")
ADMIN = Principal("u-admin", "Ada")
USER = Principal("u-user", "Pat")
OTHER = Principal("u-other", "Sam")


@pytest.fixture(autouse=True)
def no_real_services(tmp_path, monkeypatch):
    """Code under test must never touch the real systemd units, scheduled tasks,
    firewall, or linger of the machine running the tests."""
    from autoadmin import service
    from autoadmin.server import systemd
    from autoadmin.setup import host
    units = tmp_path / "systemd-user"
    monkeypatch.setattr(service, "UNIT_DIR", units)
    monkeypatch.setattr(systemd, "UNIT_DIR", units)

    def refuse(*args, **kwargs):
        raise AssertionError("a test reached a real system command; stub it")
    for name in ("start_bot_unit", "remove_unit", "bot_task_exists", "remove_bot_task",
                 "install_bot_task", "bot_task_always_on"):
        monkeypatch.setattr(service, name, refuse)
    for name in ("linger_enabled", "enable_linger", "disable_linger", "linux_firewall",
                 "run_interactive", "windows_firewall_rule_exists"):
        monkeypatch.setattr(host, name, refuse)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        runtime=tmp_path / "runtime",
        agent=AgentConfig(model="test-model", max_steps=5),
        budget=BudgetConfig(cap_usd=10.0, warn_fraction=0.8, user_block_fraction=0.05),
        # $1 per million of everything keeps the arithmetic obvious.
        pricing={"test-model": Price(1.0, 1.0, 1.0, 1.0)},
        discord=DiscordConfig(),
    )


@pytest.fixture
def services(config: Config) -> Services:
    s = Services.from_config(config)
    s.perms.set_owner(OWNER.id, OWNER.name)
    s.perms.grant_admin(ADMIN.id, ADMIN.name)
    return s


def usage(tokens: int = 0) -> NS:
    return NS(input_tokens=tokens, output_tokens=0, cache_read_input_tokens=0,
              cache_creation_input_tokens=0, iterations=None)


def text(t: str) -> NS:
    return NS(type="text", text=t)


def tool_use(name: str, args: dict, id: str = "tu1") -> NS:
    return NS(type="tool_use", name=name, input=args, id=id)


def response(*blocks: NS, stop: str = "end_turn", tokens: int = 1000) -> NS:
    return NS(content=list(blocks), stop_reason=stop, usage=usage(tokens), model="test-model")


class FakeClient:
    """Returns scripted responses and records every request."""

    def __init__(self, *responses: NS):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.beta = NS(messages=NS(create=self._create))

    async def _create(self, **kwargs):
        # Snapshot the history; the agent keeps mutating the same list.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)
