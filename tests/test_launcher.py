from __future__ import annotations

import asyncio
import ctypes
import json
import platform
import sys
from pathlib import Path

import pytest

from autoadmin.server import console, lockfile, resources, versions, winjob
from autoadmin.server.backend import LaunchSpec, ProcessState, launcher_argv
from autoadmin.server.console import ConsoleError
from autoadmin.server.launcher import Launcher, strip_prefix
from autoadmin.server.systemd import render_unit
from autoadmin.server.windows import WindowsBackend

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
    assert "-m autoadmin.server.launcher --endpoint /run/user/1/mc.sock" in unit
    assert "-- /j/bin/java -Xmx4096M -Xms1024M -jar server.jar nogui" in unit
    assert "KillMode=mixed" in unit and "MemoryMax=5632M" in unit and "TimeoutStopSec=90" in unit
    assert "StandardOutput" not in unit     # the launcher writes console.log itself


def test_endpoint_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert console.endpoint_path("mc", tmp_path) == Path("/run/user/1000/mc.sock")
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert console.endpoint_path("mc", tmp_path) == tmp_path / "mc.sock"
    monkeypatch.setattr(sys, "platform", "win32")
    assert console.endpoint_path("mc", tmp_path) == tmp_path / "mc.endpoint.json"


# -- TCP endpoint (Windows) ----------------------------------------------------------------

async def test_tcp_endpoint_needs_the_token(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    endpoint = tmp_path / "mc.endpoint.json"
    launcher = Launcher([sys.executable, str(script)], endpoint, tmp_path / "console.log",
                        tmp_path / "server.exit")
    task = await _start(launcher, endpoint)
    assert endpoint.stat().st_mode & 0o077 == 0          # owner-only: it holds the token
    assert await console.send(endpoint, "list") == ["There are 0 of a max of 20 players online: "]
    assert (await console.control(endpoint, "ping"))["stopping"] is False
    assert (await console.control(endpoint, "stats"))["uptime_s"] >= 0

    info = json.loads(endpoint.read_text())
    reader, writer = await asyncio.open_connection("127.0.0.1", info["port"])
    writer.write(json.dumps({"command": "op mallory", "token": "guess"}).encode() + b"\n")
    assert json.loads(await reader.readline()) == {"ok": False, "error": "bad token"}
    writer.close()

    await console.control(endpoint, "stop")              # how the Windows backend stops it
    assert await asyncio.wait_for(task, 10) == 0
    assert not endpoint.exists()


def test_lock_file(tmp_path):
    lock = tmp_path / "mc.lock"
    assert not lockfile.held(lock)
    f = lockfile.hold(lock)
    assert lockfile.held(lock)
    with pytest.raises(OSError):
        lockfile.hold(lock)
    f.close()                                            # what process exit does
    assert not lockfile.held(lock)


# -- Windows backend, run on Linux against the fake server ----------------------------------

HANGING_SERVER = FAKE_SERVER.replace('elif cmd == "stop":', 'elif cmd == "stop" and False:')


def _backend(tmp_path, source=FAKE_SERVER, stop_timeout_s=10):
    script = tmp_path / "fake_server.py"
    script.write_text(source)
    # `python -Xmx… -Xms… fake_server.py nogui`: python ignores unknown -X options.
    spec = LaunchSpec(workdir=tmp_path, java=Path(sys.executable), heap_mb=1024,
                      launch=[str(script)], memory_max_mb=2560, nice=5,
                      console_log=tmp_path / "console.log")
    return WindowsBackend("mc", tmp_path / "run", stop_timeout_s), spec


async def _wait_ready(backend):
    for _ in range(100):
        if (await backend.state()).active == "active":
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"never became active: {await backend.state()}")


async def test_windows_backend_lifecycle(tmp_path):
    backend, spec = _backend(tmp_path)
    assert await backend.state() == ProcessState("inactive", None, "")
    await backend.start(spec)
    await _wait_ready(backend)
    assert await backend.command("list") == ["There are 0 of a max of 20 players online: "]
    acct = await backend.accounting()
    assert acct["running"] and acct["uptime_s"] >= 0
    with pytest.raises(RuntimeError, match="already running"):
        await backend.start(spec)

    await backend.stop()
    state = await backend.state()
    assert state == ProcessState("inactive", 0, "success") and not state.running
    assert (await backend.accounting()) == {"running": False}
    assert "Stopping the server" in (tmp_path / "console.log").read_text()


async def test_windows_backend_crash_and_restart(tmp_path):
    backend, spec = _backend(tmp_path)
    await backend.start(spec)
    await _wait_ready(backend)
    with pytest.raises(ConsoleError):
        await backend.command("crash", until="never", timeout=2)
    for _ in range(50):
        if not (await backend.state()).running:
            break
        await asyncio.sleep(0.1)
    assert await backend.state() == ProcessState("failed", 3, "exit-code")
    await backend.start(spec)                            # the supervisor's restart
    await _wait_ready(backend)
    await backend.stop()


async def test_windows_backend_forces_a_hung_stop(tmp_path):
    backend, spec = _backend(tmp_path, HANGING_SERVER, stop_timeout_s=1)
    await backend.start(spec)
    await _wait_ready(backend)
    await backend.stop()
    state = await backend.state()
    assert state.result == "timeout" and not state.running


async def test_windows_backend_notices_a_vanished_launcher(tmp_path):
    backend, _ = _backend(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    # Left behind by a launcher that was killed outright, or a power cut.
    (run / "mc.endpoint.json").write_text(json.dumps({"port": 1, "token": "x", "pid": 1}))
    assert await backend.state() == ProcessState("failed", None, "signal")


def test_windows_structs_match_the_x64_layout():
    assert ctypes.sizeof(winjob.EXTENDED_LIMIT) == 144
    assert ctypes.sizeof(winjob.BASIC_ACCOUNTING) == 48
    assert ctypes.sizeof(winjob.PROCESS_MEMORY_COUNTERS) == 72
    assert ctypes.sizeof(resources._MEMORYSTATUSEX) == 64


def test_java_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    assert versions.java_platform() == ("windows", "x64")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform, "machine", lambda: "aarch64")
    assert versions.java_platform() == ("linux", "aarch64")


def test_exit_reasons(monkeypatch):
    from autoadmin.server.manager import _exit_reason
    assert _exit_reason(137) == "was killed by signal 9"
    assert _exit_reason(3) == "exited with status 3"
    monkeypatch.setattr(sys, "platform", "win32")
    assert _exit_reason(137) == "exited with status 137"
    assert _exit_reason(0xC0000005) == "exited with status 0xc0000005"


def test_backups_skip_session_lock():
    import tarfile
    from autoadmin.server.backups import _skip_lock
    assert _skip_lock(tarfile.TarInfo("world/session.lock")) is None
    assert _skip_lock(tarfile.TarInfo("world/level.dat")) is not None
