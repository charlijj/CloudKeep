"""Async TCP-to-WebSocket relay for VNC traffic.

Pure transport layer: owns one VNC TCP connection and shuttles raw bytes
between it and a WebSocket. No HTTP, auth, or RFB parsing here — the
browser's RFB.js speaks the protocol; this class only moves bytes.
"""
import asyncio
import logging
import time
from contextlib import suppress
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("cloudkeep.bridge")

# 32 KB: large enough for framebuffer throughput, small enough to keep
# mouse/keyboard input latency low.
_CHUNK = 32768

# Disconnects that are part of normal teardown, not real errors.
_EXPECTED = (
    WebSocketDisconnect,
    ConnectionResetError,
    asyncio.IncompleteReadError,
    asyncio.CancelledError,
)


class VNCBridge:
    """Bidirectional byte relay between a VNC server and a WebSocket."""

    def __init__(self, vnc_host: str, vnc_port: int) -> None:
        self.vnc_host = vnc_host
        self.vnc_port = vnc_port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._tasks: list[asyncio.Task] = []

    async def connect(self) -> None:
        """Open the TCP connection to the VNC server.

        Raises ConnectionError if the VNC server cannot be reached.
        """
        try:
            self._reader, self._writer = await asyncio.open_connection(
                self.vnc_host, self.vnc_port
            )
        except OSError as exc:
            raise ConnectionError(
                f"VNC unreachable at {self.vnc_host}:{self.vnc_port}"
            ) from exc

    async def relay(self, websocket: WebSocket) -> None:
        """Relay bytes both ways until either side closes.

        Whichever direction finishes first, the other is cancelled — so a
        browser tab close or a VNC EOF tears the whole bridge down.
        """
        started = time.monotonic()
        logger.info("bridge open -> %s:%s", self.vnc_host, self.vnc_port)
        self._tasks = [
            asyncio.create_task(self._ws_to_vnc(websocket)),
            asyncio.create_task(self._vnc_to_ws(websocket)),
        ]
        try:
            # FIRST_COMPLETED, not gather(): gather() would wait for *both*
            # tasks, hanging when one side closes but the other is idle.
            _, pending = await asyncio.wait(
                self._tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
        finally:
            await self.close()
            logger.info("bridge closed (%.1fs)", time.monotonic() - started)

    async def _ws_to_vnc(self, websocket: WebSocket) -> None:
        """Forward browser -> VNC. Binary frames only."""
        while True:
            data = await websocket.receive_bytes()
            self._writer.write(data)
            await self._writer.drain()  # backpressure if the VNC server lags

    async def _vnc_to_ws(self, websocket: WebSocket) -> None:
        """Forward VNC -> browser."""
        while True:
            data = await self._reader.read(_CHUNK)
            if not data:  # EOF — VNC server closed
                break
            await websocket.send_bytes(data)

    async def close(self) -> None:
        """Cancel relay tasks and close the TCP connection. Idempotent."""
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            with suppress(*_EXPECTED):
                await task
        self._tasks = []
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()
            with suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None
