"""Compute and storage usage of the host, the server process, and the runtime dir."""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

from . import properties

MB = 1024 * 1024


def _dir_size(path: Path) -> int:
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _meminfo() -> dict[str, int]:
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) // 1024
    return out


def host() -> dict:
    mem = _meminfo()
    load = os.getloadavg()
    disk = shutil.disk_usage(Path.home())
    return {
        "cpu_cores": os.cpu_count(),
        "load_avg_1m_5m_15m": [round(x, 2) for x in load],
        "memory_total_mb": mem.get("MemTotal"),
        "memory_available_mb": mem.get("MemAvailable"),
        "disk_free_gb": round(disk.free / 1024 ** 3, 1),
    }


def storage(runtime: Path) -> dict:
    server = runtime / "server"
    level = properties.read(server / "server.properties").get("level-name", "world")
    world = server / level
    world_mb = _dir_size(world) / MB if world.exists() else 0
    server_mb = _dir_size(server) / MB if server.exists() else 0
    old = sum(_dir_size(p) for p in runtime.glob("server.old-*")) / MB
    sizes = {
        "world": world_mb,
        "server_install_excl_world": server_mb - world_mb,
        "previous_installs": old,
        "java_runtimes": _dir_size(runtime / "java") / MB,
        "download_cache": _dir_size(runtime / "cache") / MB,
        "logs_and_incidents": (_dir_size(runtime / "logs") + _dir_size(runtime / "incidents")) / MB,
    }
    sizes = {k: round(v, 1) for k, v in sizes.items()}
    sizes["total"] = round(sum(sizes.values()), 1)
    return {"runtime_mb": sizes}


async def process(unit, heap_mb: int | None) -> dict:
    first = await unit.accounting()
    if not first.get("running"):
        return {"running": False}
    t0 = time.monotonic()
    await asyncio.sleep(1)
    second = await unit.accounting()
    cores = os.cpu_count() or 1
    cpu_now = (second["cpu_ns"] - first["cpu_ns"]) / ((time.monotonic() - t0) * 1e9) * 100
    up = second["uptime_s"] or 1
    return {
        "running": True,
        "uptime_min": round(up / 60, 1),
        "memory_mb": round(second["memory"] / MB),
        "memory_peak_mb": round(second["memory_peak"] / MB),
        "memory_limit_mb": round(second["memory_max"] / MB) if second["memory_max"] else None,
        "heap_setting_mb": heap_mb,
        # 100% = one full core; the host has `cores` of them.
        "cpu_percent_now": round(cpu_now, 1),
        "cpu_percent_avg_since_start": round(second["cpu_ns"] / (up * 1e9) * 100, 1),
        "cpu_cores_on_host": cores,
    }
