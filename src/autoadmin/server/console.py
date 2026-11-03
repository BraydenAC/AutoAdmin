"""Client for the launcher's private console channel (replaces RCON).

One request per connection, newline-delimited JSON:
  -> {"command": "list", "until": null, "timeout": 2.0}
  <- {"ok": true, "output": ["There are 0 of a max of 20 players online:"]}
Output is the console lines the server printed after the command, with log
prefixes removed. `until` (a regex) waits for a specific line, e.g. the end of
a save. `{"control": "ping" | "stop" | "stats"}` asks about the launcher itself.

The endpoint is a Unix socket (`*.sock`, private by its directory), or on
Windows a localhost TCP port named in an owner-only file (`*.json`) together
with a token every request must carry. Stdlib only: the launcher imports this
module too.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

DEFAULT_TIMEOUT = 2.0
MAX_TIMEOUT = 300.0
CONNECT_TIMEOUT = 5.0


class ConsoleError(Exception):
    pass


def endpoint_path(unit_name: str, fallback_dir: Path) -> Path:
    if sys.platform == "win32":
        return fallback_dir / f"{unit_name}.endpoint.json"
    # $XDG_RUNTIME_DIR is private to the user (0700) and short enough for the
    # 108-byte socket path limit; the jail is the fallback.
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / f"{unit_name}.sock" if base else fallback_dir / f"{unit_name}.sock"


def is_tcp(endpoint: Path) -> bool:
    return endpoint.suffix == ".json"


def check_command(command: str) -> str:
    """One console line. A newline would smuggle in a second, unchecked command."""
    command = command.strip().removeprefix("/")
    if not command:
        raise ConsoleError("empty command")
    if any(c in command for c in "\r\n\0"):
        raise ConsoleError("commands must be a single line")
    return command


async def _connect(endpoint: Path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict]:
    if not is_tcp(endpoint):
        return (*await asyncio.open_unix_connection(str(endpoint)), {})
    info = json.loads(endpoint.read_text())
    return (*await asyncio.open_connection("127.0.0.1", int(info["port"])),
            {"token": info["token"]})


async def request(endpoint: Path, req: dict, answer_timeout: float) -> dict:
    """Send one request; returns the response dict, raising ConsoleError if not ok."""
    try:
        reader, writer, auth = await asyncio.wait_for(_connect(endpoint), CONNECT_TIMEOUT)
    except (OSError, TimeoutError, ValueError, KeyError) as e:
        raise ConsoleError(f"server console unavailable: {e or type(e).__name__}") from e
    try:
        writer.write(json.dumps({**req, **auth}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), answer_timeout)
    except (OSError, TimeoutError) as e:
        raise ConsoleError(f"server console didn't answer: {e or type(e).__name__}") from e
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
    if not line:
        raise ConsoleError("server console closed the connection")
    resp = json.loads(line)
    if not resp.get("ok"):
        raise ConsoleError(resp.get("error") or "console request failed")
    return resp


async def send(endpoint: Path, command: str, *, until: str | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    command = check_command(command)
    resp = await request(endpoint, {"command": command, "until": until, "timeout": timeout},
                         timeout + 5)
    return resp["output"]


async def control(endpoint: Path, action: str) -> dict:
    return await request(endpoint, {"control": action}, 5)
