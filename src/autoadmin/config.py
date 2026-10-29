"""Configuration loading and the runtime directory jail."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True)
class AgentConfig:
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_tokens: int = 16000
    max_steps: int = 25


@dataclass(frozen=True)
class InvestigatorConfig:
    model: str = "claude-haiku-4-5"
    thinking_budget: int = 4000
    max_tokens: int = 8000
    max_steps: int = 8


@dataclass(frozen=True)
class BudgetConfig:
    cap_usd: float = 10.0
    warn_fraction: float = 0.8
    user_block_fraction: float = 0.05


@dataclass(frozen=True)
class ServerConfig:
    unit_name: str = "autoadmin-mc"
    memory_mb: int = 6144              # default -Xmx; adjustable via chat
    min_host_free_mb: int = 4096       # refuse heaps that leave the host less than this
    memory_overhead_mb: int = 1536     # systemd MemoryMax = heap + this
    nice: int = 5                      # lower CPU priority than the desktop
    server_port: int = 25565
    rcon_port: int = 25575
    start_timeout_s: int = 300
    stop_timeout_s: int = 90
    install_timeout_s: int = 900


@dataclass(frozen=True)
class DiscordConfig:
    guild_id: int = 0
    channel_id: int = 0
    token_env: str = "AUTOADMIN_DISCORD_TOKEN"


@dataclass(frozen=True)
class Config:
    runtime: Path
    agent: AgentConfig
    budget: BudgetConfig
    pricing: dict[str, Price]
    discord: DiscordConfig
    investigator: InvestigatorConfig = InvestigatorConfig()
    server: ServerConfig = ServerConfig()
    cli_owner: str = "owner"
    source: Path | None = field(default=None, compare=False)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        path = Path(path).resolve()
        with path.open("rb") as f:
            raw = tomllib.load(f)
        runtime = Path(raw.get("paths", {}).get("runtime", "runtime"))
        if not runtime.is_absolute():
            runtime = path.parent / runtime
        return cls(
            runtime=runtime.resolve(),
            agent=AgentConfig(**raw.get("agent", {})),
            budget=BudgetConfig(**raw.get("budget", {})),
            pricing={m: Price(**p) for m, p in raw.get("pricing", {}).items()},
            discord=DiscordConfig(**raw.get("discord", {})),
            investigator=InvestigatorConfig(**raw.get("investigator", {})),
            server=ServerConfig(**raw.get("server", {})),
            cli_owner=raw.get("cli", {}).get("owner", "owner"),
            source=path,
        )


class JailError(ValueError):
    pass


class Jail:
    """Resolves paths and refuses anything outside the runtime directory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, *parts: str | Path) -> Path:
        p = self.root.joinpath(*parts).resolve()
        if p != self.root and self.root not in p.parents:
            raise JailError(f"path escapes runtime directory: {p}")
        return p
