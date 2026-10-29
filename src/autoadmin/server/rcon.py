"""Minimal async client for the Source RCON protocol Minecraft speaks."""

from __future__ import annotations

import asyncio
import struct

LOGIN, COMMAND, RESPONSE = 3, 2, 0


class RconError(Exception):
    pass


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10):
        self.host, self.port, self.password, self.timeout = host, port, password, timeout

    @staticmethod
    def _packet(req_id: int, kind: int, body: str) -> bytes:
        payload = struct.pack("<ii", req_id, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(payload)) + payload

    async def _read(self, reader: asyncio.StreamReader) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", await reader.readexactly(4))
        data = await reader.readexactly(length)
        req_id, kind = struct.unpack("<ii", data[:8])
        return req_id, kind, data[8:-2].decode(errors="replace")

    async def command(self, cmd: str) -> str:
        try:
            return await asyncio.wait_for(self._command(cmd), self.timeout)
        except (OSError, asyncio.IncompleteReadError, TimeoutError) as e:
            raise RconError(f"RCON unavailable: {e or type(e).__name__}") from e

    async def _command(self, cmd: str) -> str:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            writer.write(self._packet(1, LOGIN, self.password))
            await writer.drain()
            req_id, _, _ = await self._read(reader)
            if req_id == -1:
                raise RconError("RCON authentication failed")
            writer.write(self._packet(2, COMMAND, cmd))
            await writer.drain()
            _, _, body = await self._read(reader)
            return body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
