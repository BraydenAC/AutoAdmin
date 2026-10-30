"""World profile, backup, and mod tools."""

from __future__ import annotations

import json
from typing import Any

from ..auth import Perm, Principal
from ..server.manager import LOADERS, ServerError
from .confirm import confirmed
from .registry import Registry, ToolContext, ToolError

WORLD_ACTIONS: dict[str, Perm | tuple[Perm, ...]] = {
    "list": Perm.CHAT,
    "list_backups": Perm.CHAT,
    "create": Perm.WORLDS_MANAGE,
    "switch": Perm.WORLDS_MANAGE,
    "backup": Perm.BACKUPS_CREATE,
    "restore": Perm.BACKUPS_RESTORE,
    "delete": Perm.WORLDS_DELETE,
    "delete_backup": Perm.WORLDS_DELETE,
    "set_backup_retention": Perm.ADMINS_MANAGE,   # owner only
}

MOD_ACTIONS: dict[str, Perm | tuple[Perm, ...]] = {
    "search": Perm.CHAT,
    "list": Perm.CHAT,
    "install": Perm.MODS_MANAGE,
    "install_url": Perm.MODS_MANAGE,
    "install_upload": Perm.MODS_MANAGE,
    "remove": Perm.MODS_MANAGE,
    # The supervisor's crash recovery may toggle mods as a reversible fix.
    "disable": (Perm.MODS_MANAGE, Perm.CRASH_RECOVERY),
    "enable": (Perm.MODS_MANAGE, Perm.CRASH_RECOVERY),
}


def _by_action(table: dict[str, Perm | tuple[Perm, ...]]):
    def perm(args: dict[str, Any]) -> Perm | tuple[Perm, ...]:
        action = args.get("action")
        if action not in table:
            raise ToolError(f"unknown action '{action}'")
        return table[action]
    return perm


def _need(value: Any, what: str, action: str) -> Any:
    if value in ("", 0, [], None):
        raise ToolError(f"{action} needs {what}")
    return value


def register(r: Registry) -> None:

    @r.tool("worlds",
            "World profiles and backups. Each world is a separate server folder with its own "
            "version, loader, mods, and save. Unused arguments: '' or 0.\n"
            "list | create(name, minecraft_version, loader) | switch(name) | delete(name)* | "
            "backup(name) | list_backups(name) | restore(name, backup_id)* | "
            "delete_backup(name, backup_id)* | set_backup_retention(count; owner only).\n"
            "* needs the requester's confirmation on a later message; call once, ask, then call "
            "again with the same arguments. The server must be stopped to switch, or to delete/"
            "restore the active world. create blocks until installed and downloads Java as needed.",
            permission=_by_action(WORLD_ACTIONS),
            properties={
                "action": {"type": "string", "enum": list(WORLD_ACTIONS)},
                "name": {"type": "string", "description": "World name: lowercase, digits, - and _"},
                "minecraft_version": {"type": "string",
                                      "description": "For create: e.g. 1.21.1, 26.3, latest"},
                "loader": {"type": "string", "enum": ["", *LOADERS]},
                "backup_id": {"type": "string"},
                "count": {"type": "integer"},
            })
    async def worlds(ctx: ToolContext, action: str, name: str, minecraft_version: str,
                     loader: str, backup_id: str, count: int) -> str:
        s = ctx.services
        who = str(ctx.principal)
        try:
            match action:
                case "list":
                    return json.dumps({"worlds": s.server.list_worlds(),
                                       "backup_retention": s.backups.retention})
                case "create":
                    _need(name, "a name", action)
                    _need(minecraft_version, "a minecraft_version", action)
                    _need(loader, "a loader", action)
                    return await s.server.install(minecraft_version, loader, who, ctx.say,
                                                  world=name)
                case "switch":
                    return await s.server.switch_world(_need(name, "a name", action))
                case "delete":
                    _need(name, "a name", action)
                    s.server.world_dir(name)
                    if ask := confirmed(ctx, "delete_world", {"name": name},
                                        f"permanently delete world '{name}' (backups are kept)"):
                        return ask
                    return await s.server.delete_world(name)
                case "backup":
                    world = name or s.server.active_world()
                    _need(world, "a name", action)
                    meta = await s.backups.create(world, "manual", "requested", who)
                    return (f"Backed up '{world}' as {meta['id']} ({meta['size_mb']} MB). "
                            "Manual backups are kept until deleted.")
                case "list_backups":
                    world = name or s.server.active_world()
                    _need(world, "a name", action)
                    return json.dumps([{k: b[k] for k in ("id", "kind", "reason", "size_mb",
                                                          "minecraft_version", "loader")}
                                       for b in s.backups.list(world)])
                case "restore":
                    _need(name, "a name", action)
                    _need(backup_id, "a backup_id", action)
                    meta = s.backups.get(name, backup_id)
                    if ask := confirmed(ctx, "restore", {"name": name, "id": backup_id},
                                        f"restore world '{name}' to backup {backup_id} "
                                        f"({meta['reason']}); current state is backed up first"):
                        return ask
                    return await s.backups.restore(name, backup_id, who, ctx.say)
                case "delete_backup":
                    _need(name, "a name", action)
                    _need(backup_id, "a backup_id", action)
                    s.backups.get(name, backup_id)
                    if ask := confirmed(ctx, "delete_backup", {"name": name, "id": backup_id},
                                        f"permanently delete backup {backup_id} of '{name}'"):
                        return ask
                    return s.backups.delete(name, backup_id)
                case "set_backup_retention":
                    return s.backups.set_retention(_need(count, "a count", action))
        except ServerError as e:
            raise ToolError(str(e))
        raise ToolError(f"unknown action '{action}'")

    @r.tool("mods",
            "Mods for the active world (Fabric/NeoForge). Unused arguments: '' or [].\n"
            "search(query): Modrinth results compatible with this world; pick the exact slug.\n"
            "install(names=[slugs]): installs matching versions plus required dependencies; "
            "client-only mods are refused. Reports each mod. If a mod fails on Modrinth, try "
            "install_url(url) with a direct jar link, or ask the requester to upload the jar "
            "and use install_upload(names=[filename]).\n"
            "list | remove(names) | disable(names) | enable(names). Changes are backed up first "
            "and apply on the next server start.",
            permission=_by_action(MOD_ACTIONS),
            properties={
                "action": {"type": "string", "enum": list(MOD_ACTIONS)},
                "query": {"type": "string"},
                "names": {"type": "array", "items": {"type": "string"},
                          "description": "Modrinth slugs, installed mod names, or an uploaded filename"},
                "url": {"type": "string"},
            })
    async def mods(ctx: ToolContext, action: str, query: str, names: list[str], url: str) -> str:
        m = ctx.services.mods
        who = str(ctx.principal)
        try:
            match action:
                case "search":
                    return json.dumps(await m.search(_need(query, "a query", action)))
                case "list":
                    return json.dumps(m.list())
                case "install":
                    return await m.install(_need(names, "names", action), who, ctx.say)
                case "install_url":
                    _need(url, "a url", action)
                    if not url.startswith("https://"):
                        raise ToolError("only https:// links are accepted")
                    return await m.install_file(url, url.rsplit("/", 1)[-1].split("?")[0],
                                                "url", who)
                case "install_upload":
                    filename = _need(names, "the uploaded filename", action)[0]
                    upload = ctx.conv_state.get("attachments", {}).get(filename)
                    if upload is None:
                        raise ToolError(f"no attachment named {filename} in this conversation")
                    perms = ctx.services.perms
                    up = Principal(upload["by"], perms.name_of(upload["by"]))
                    if not perms.check(up, Perm.MODS_MANAGE).allowed:
                        raise ToolError(f"{up.name} uploaded it but lacks mods.manage; "
                                        "uploads are only accepted from users who can manage mods")
                    return await m.install_file(upload["url"], filename, "upload", who)
                case "remove":
                    return await m.remove(_need(names, "names", action), who)
                case "disable" | "enable":
                    return await m.set_enabled(_need(names, "names", action), action == "enable")
        except ServerError as e:
            raise ToolError(str(e))
        raise ToolError(f"unknown action '{action}'")
