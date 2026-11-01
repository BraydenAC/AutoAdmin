from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from autoadmin.server import console
from autoadmin.server.backend import LaunchSpec, launcher_argv
from autoadmin.server.console import ConsoleError
from autoadmin.server.launcher import Launcher, strip_prefix
from autoadmin.server.systemd import render_unit

# Behaves like a Minecraft server console: log-prefixed output, `stop` saves and
# exits 0, `save-all flush` finishes a little after the command returns.
FAKE_SERVER = r'''
import sys, time
def log(msg):
    print(f"[12:00:00] [Server thread/INFO]: {msg}", flush=True)
log("Done (1.234s)! For help, type \"help\"")
for line in sys.stdin:
    cmd = line.strip()
    if cmd == "list":
        log("There are 0 of a max of 20 players online: ")
    elif cmd == "save-all flush":
        log("Saving the game (this may take a moment!)")
        time.sleep(0.5)
        log("Saved the game")
    elif cmd == "quiet":
        pass
    elif cmd == "stop":
        log("Stopping the server")
        sys.exit(0)
    elif cmd == "crash":
        sys.exit(3)
    else:
        log(f"Unknown or incomplete command: {cmd}")
'''


@pytest.fixture
def launched(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    sock = tmp_path / "c.sock"
    launcher = Launcher([sys.executable, str(script)], sock, tmp_path / "console.log",
                        tmp_path / "server.exit")
    return launcher, sock, tmp_path


async def _start(launcher: Launcher, sock: Path) -> asyncio.Task:
    task = asyncio.create_task(launcher.run())
    for _ in range(100):
        if sock.exists():
            return task
        await asyncio.sleep(0.05)
    raise AssertionError("launcher never opened its socket")


async def test_commands_roundtrip_and_clean_stop(launched):
    launcher, sock, tmp = launched
    task = await _start(launcher, sock)
    assert await console.send(sock, "/list") == ["There are 0 of a max of 20 players online: "]
    # Waits past the immediate output for the line that marks the end.
    out = await console.send(sock, "save-all flush", until="Saved the game", timeout=10)
    assert out == ["Saving the game (this may take a moment!)", "Saved the game"]
    assert await console.send(sock, "quiet", timeout=0.5) == []
    with pytest.raises(ConsoleError, match="timed out"):
        await console.send(sock, "quiet", until="never", timeout=0.5)

    launcher.request_stop()          # what SIGTERM from `systemctl stop` does
    assert await asyncio.wait_for(task, 10) == 0
    assert not sock.exists()
    assert json.loads((tmp / "server.exit").read_text())["stop_requested"] is True
    log = (tmp / "console.log").read_text()
    assert "Done (1.234s)!" in log and "Stopping the server" in log


async def test_crash_exit_status_is_passed_through(launched):
    launcher, sock, tmp = launched
    task = await _start(launcher, sock)
    with pytest.raises(ConsoleError):
        await console.send(sock, "crash", until="never", timeout=2)
    assert await asyncio.wait_for(task, 10) == 3
    assert json.loads((tmp / "server.exit").read_text())["exit_status"] == 3


async def test_multiline_commands_are_refused(launched):
    launcher, sock, _ = launched
    task = await _start(launcher, sock)
    with pytest.raises(ConsoleError, match="single line"):
        await console.send(sock, "list\nop mallory")
    # Also refused by the launcher itself, whatever the client does.
    with pytest.raises(ConsoleError, match="single line"):
        await launcher.command("list\nop mallory", None, 1)
    launcher.request_stop()
    await asyncio.wait_for(task, 10)


async def test_unavailable_console(tmp_path):
    with pytest.raises(ConsoleError, match="unavailable"):
        await console.send(tmp_path / "missing.sock", "list")


def test_strip_prefix_formats():
    assert strip_prefix("[12:00:00] [Server thread/INFO]: hi: there") == "hi: there"
    assert strip_prefix("[12:00:00] [Server thread/INFO] [minecraft/MinecraftServer]: hi") == "hi"
    assert strip_prefix("no prefix") == "no prefix"


def test_unit_runs_launcher_without_rcon(tmp_path):
    spec = LaunchSpec(workdir=tmp_path, java=Path("/j/bin/java"), heap_mb=4096,
                      launch=["-jar", "server.jar"], memory_max_mb=5632, nice=5,
                      console_log=tmp_path / "console.log")
    argv = launcher_argv(Path("/run/user/1/mc.sock"), spec.console_log, tmp_path / "mc.exit", spec)
    unit = render_unit(spec, argv, stop_timeout_s=90)
    assert "-m autoadmin.server.launcher --socket /run/user/1/mc.sock" in unit
    assert "-- /j/bin/java -Xmx4096M -Xms1024M -jar server.jar nogui" in unit
    assert "KillMode=mixed" in unit and "MemoryMax=5632M" in unit and "TimeoutStopSec=90" in unit
    assert "StandardOutput" not in unit     # the launcher writes console.log itself


def test_socket_path_prefers_runtime_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert console.socket_path("mc", tmp_path) == Path("/run/user/1000/mc.sock")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert console.socket_path("mc", tmp_path) == tmp_path / "mc.sock"
