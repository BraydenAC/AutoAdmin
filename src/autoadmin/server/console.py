"""Client for the launcher's private console channel (replaces RCON).

One request per connection, newline-delimited JSON:
  -> {"command": "list", "until": null, "timeout": 2.0}
  <- {"ok": true, "output": ["There are 0 of a max of 20 players online:"]}
Output is the console lines the server printed after the command, with log
prefixes removed. `until` (a regex) waits for a specific line, e.g. the end of
a save. Stdlib only: the launcher imports this module too.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

DEFAULT_TIMEOUT = 2.0
MAX_TIMEOUT = 300.0


class ConsoleError(Exception):
    pass


def socket_path(unit_name: str, fallback_dir: Path) -> Path:
    """$XDG_RUNTIME_DIR is private to the user (0700) and short enough for the
    108-byte socket path limit; the jail is the fallback."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / f"{unit_name}.sock" if base else fallback_dir / f"{unit_name}.sock"


def check_command(command: str) -> str:
    """One console line. A newline would smuggle in a second, unchecked command."""
    command = command.strip().removeprefix("/")
    if not command:
        raise ConsoleError("empty command")
    if any(c in command for c in "\r\n\0"):
        raise ConsoleError("commands must be a single line")
    return command


async def send(path: Path, command: str, *, until: str | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> list[str]:
    command = check_command(command)
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), 5)
    except (OSError, TimeoutError) as e:
        raise ConsoleError(f"server console unavailable: {e or type(e).__name__}") from e
    try:
        writer.write(json.dumps({"command": command, "until": until,
                                 "timeout": timeout}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout + 5)
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
        raise ConsoleError(resp.get("error") or "console command failed")
    return resp["output"]
