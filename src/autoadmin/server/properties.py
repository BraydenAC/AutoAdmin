"""server.properties reading and validated editing."""

from __future__ import annotations

from pathlib import Path

LEVEL_TYPES = {
    "normal": "minecraft:normal",
    "flat": "minecraft:flat",
    "superflat": "minecraft:flat",
    "large_biomes": "minecraft:large_biomes",
    "amplified": "minecraft:amplified",
}

_BOOL = ("true", "false")


def _int(lo: int, hi: int):
    def check(v: str) -> str:
        if not v.lstrip("-").isdigit() or not lo <= int(v) <= hi:
            raise ValueError(f"must be an integer from {lo} to {hi}")
        return v
    return check


def _choice(*opts: str):
    def check(v: str) -> str:
        if v not in opts:
            raise ValueError(f"must be one of: {', '.join(opts)}")
        return v
    return check


def _level_type(v: str) -> str:
    key = v.removeprefix("minecraft:")
    if key not in LEVEL_TYPES:
        raise ValueError(f"must be one of: {', '.join(sorted(set(LEVEL_TYPES) - {'superflat'}))}")
    return LEVEL_TYPES[key]


def _text(max_len: int):
    def check(v: str) -> str:
        if len(v) > max_len or "\n" in v:
            raise ValueError(f"must be one line, at most {max_len} characters")
        return v
    return check


# Keys the agent may change. RCON, ports, and query settings are managed by AutoAdmin.
EDITABLE = {
    "level-seed": _text(64),
    "level-type": _level_type,
    "level-name": _text(64),
    "gamemode": _choice("survival", "creative", "adventure", "spectator"),
    "force-gamemode": _choice(*_BOOL),
    "difficulty": _choice("peaceful", "easy", "normal", "hard"),
    "hardcore": _choice(*_BOOL),
    "pvp": _choice(*_BOOL),
    "generate-structures": _choice(*_BOOL),
    "spawn-monsters": _choice(*_BOOL),
    "allow-flight": _choice(*_BOOL),
    "allow-nether": _choice(*_BOOL),
    "enable-command-block": _choice(*_BOOL),
    "max-players": _int(1, 200),
    "view-distance": _int(3, 32),
    "simulation-distance": _int(3, 32),
    "spawn-protection": _int(0, 64),
    "motd": _text(120),
    "white-list": _choice(*_BOOL),
    "enforce-whitelist": _choice(*_BOOL),
    "online-mode": _choice(*_BOOL),
    "generator-settings": _text(2000),
}


def read(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def write(path: Path, values: dict[str, str]) -> None:
    """Merge `values` into the file, keeping other keys and their order."""
    current = read(path)
    current.update(values)
    body = "\n".join(f"{k}={v}" for k, v in current.items())
    path.write_text("#Minecraft server properties (managed by AutoAdmin)\n" + body + "\n")


def validate(changes: dict[str, str]) -> dict[str, str]:
    """Return normalized values, or raise ValueError naming every bad key."""
    out, errors = {}, []
    for k, v in changes.items():
        if k not in EDITABLE:
            errors.append(f"{k}: not editable")
            continue
        try:
            out[k] = EDITABLE[k](str(v).strip())
        except ValueError as e:
            errors.append(f"{k}: {e}")
    if errors:
        raise ValueError("; ".join(errors))
    return out
