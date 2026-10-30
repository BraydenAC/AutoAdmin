"""The bundle of long-lived subsystems that tools and the router share."""

from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditLog
from .auth import PermissionStore
from .budget import Budget
from .config import Config, Jail
from .server.backups import BackupStore, Settings
from .server.incidents import IncidentStore
from .server.manager import ServerManager
from .server.mods import ModManager


@dataclass
class Services:
    config: Config
    jail: Jail
    perms: PermissionStore
    budget: Budget
    audit: AuditLog
    incidents: IncidentStore
    server: ServerManager
    backups: BackupStore
    mods: ModManager

    @classmethod
    def from_config(cls, config: Config) -> Services:
        jail = Jail(config.runtime)
        perms = PermissionStore(jail.path("permissions.json"))
        incidents = IncidentStore(jail.path("incidents"), jail.root)
        server = ServerManager(config, jail, incidents)
        backups = BackupStore(jail, server, Settings(jail.path("settings.json")))
        return cls(
            config=config,
            jail=jail,
            perms=perms,
            budget=Budget(jail.path("state.db"), config.budget, config.pricing, perms),
            audit=AuditLog(jail.path("audit.log")),
            incidents=incidents,
            server=server,
            backups=backups,
            mods=ModManager(server, backups),
        )
