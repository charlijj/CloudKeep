"""Live serial-console streaming — a VM's boot log to the browser.

controld runs unprivileged (the `app` user), so it cannot read the root-owned
console PTY logs under /var/lib/libvirt. Instead it attaches to the domain's
serial console through the libvirt API (`openConsole` + a virStream), which a
member of the `libvirt` group is allowed to do. The domain XML defines a
`<console type='pty'>` (serial port 0); the guest's GRUB routes the kernel +
systemd boot stream to `ttyS0`, so this surfaces real boot progress without
opening any network surface on the guest.

Design:
  * Attaches on demand — only while a Logs panel is open — so an idle fleet
    costs nothing (no always-on buffering).
  * libvirt stream `recv` is a blocking C call, so the read loop runs on a
    daemon thread that owns its OWN libvirt connection (connections are not
    thread-safe to share); bytes are handed to the event loop via a queue with
    `call_soon_threadsafe`.
  * The pump races "queue -> browser" against "browser disconnected"; whichever
    finishes first tears the other down. ANSI escape sequences are stripped so
    the panel shows clean text.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading

from config import settings

logger = logging.getLogger("cloudkeep.console")

# CSI / SGR escape sequences (colour, cursor moves) -> stripped for a plain
# read-only text panel. Matches ESC [ ... <final byte>.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# Synthetic boot trace for LIBVIRT_URI=fake so the feature is exercisable
# end-to-end without real KVM.
_FAKE_LOG = [
    "[    0.000000] Linux version 6.8.0-generic (kvm guest)",
    "[    0.514203] systemd[1]: Detected virtualization kvm.",
    "[    1.203774] cloud-init disabled by /etc/cloud/cloud-init.disabled",
    "[    2.881190] cloudkeep-firstboot.service: growing root filesystem…",
    "[    3.402551] EXT4-fs (vda1): resized filesystem to 16777216 blocks",
    "[    4.998120] systemd[1]: Reached target Multi-User System.",
    "[    6.140882] cloudkeep-vnc.service: starting TigerVNC :1 …",
    "[    7.770015] CloudKeep desktop ready — VNC listening on :5901",
]


async def serve_console(websocket, name: str) -> None:
    """Accept the WS and stream domain `name`'s serial console until either
    side closes. Never raises out to the caller."""
    await websocket.accept()
    if settings.use_fake:
        await _serve_fake(websocket)
        return

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    reader = _Reader(name, queue, loop)
    reader.start()
    try:
        await _pump(websocket, queue)
    finally:
        reader.stop()
        with contextlib.suppress(Exception):
            await websocket.close()


async def _pump(websocket, queue: asyncio.Queue) -> None:
    """Forward bytes to the browser; stop when the reader ends or the browser
    disconnects (whichever happens first)."""
    async def to_browser() -> None:
        while True:
            data = await queue.get()
            if data is None:                 # reader signalled EOF / error
                return
            text = _ANSI.sub("", data.decode("utf-8", "replace"))
            await websocket.send_text(text)

    async def watch_close() -> None:
        # The panel is read-only; we never act on inbound frames — receiving
        # only tells us when the browser goes away (any frame or a close).
        with contextlib.suppress(Exception):
            while True:
                await websocket.receive_text()

    send_task = asyncio.create_task(to_browser())
    watch_task = asyncio.create_task(watch_close())
    done, pending = await asyncio.wait(
        {send_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        # Consume any exception (e.g. send on a closed socket) so it isn't
        # logged as "never retrieved"; the stream is ending regardless.
        with contextlib.suppress(Exception):
            task.result()


async def _serve_fake(websocket) -> None:
    """Emit the synthetic boot trace, then idle until the browser disconnects."""
    try:
        for line in _FAKE_LOG:
            await websocket.send_text(line + "\n")
            await asyncio.sleep(0.6)
        while True:                          # raises when the browser closes
            await websocket.receive_text()
    except Exception:
        return
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


class _Reader(threading.Thread):
    """Daemon thread: opens its own libvirt connection, attaches to the domain
    console, and pushes raw bytes onto the event loop's queue."""

    def __init__(self, name: str, queue: asyncio.Queue,
                 loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(daemon=True, name=f"console-{name}")
        self._name = name
        self._queue = queue
        self._loop = loop
        self._conn = None
        self._stream = None
        self._stop = False
        self._cleanup_lock = threading.Lock()

    def _emit(self, data) -> None:
        # Hop back onto the event-loop thread; the queue is not thread-safe.
        self._loop.call_soon_threadsafe(self._queue.put_nowait, data)

    def run(self) -> None:
        import libvirt
        try:
            self._conn = libvirt.open(settings.LIBVIRT_URI)
            if self._conn is None:
                raise RuntimeError("cannot open libvirt connection")
            dom = self._conn.lookupByName(self._name)
            self._stream = self._conn.newStream(0)        # blocking stream
            dom.openConsole(None, self._stream,
                            libvirt.VIR_DOMAIN_CONSOLE_FORCE)
            while not self._stop:
                try:
                    data = self._stream.recv(4096)
                except libvirt.libvirtError:
                    break                                 # closed/aborted -> done
                if data == -2:               # would-block (non-blocking stream)
                    continue
                if not data:                 # EOF (domain console closed)
                    break
                self._emit(data)
        except Exception as exc:
            logger.info("console reader ended vm=%s: %s", self._name, exc)
        finally:
            self._emit(None)                 # tell the pump to stop
            self._close()

    def stop(self) -> None:
        """Called from the event loop. Closing the stream/connection unblocks a
        recv() parked on the reader thread, so the loop exits promptly."""
        self._stop = True
        self._close()

    def _close(self) -> None:
        # Reached from both threads (stop() and run()'s finally); serialise so
        # the stream/connection are torn down exactly once.
        with self._cleanup_lock:
            if self._stream is not None:
                stream, self._stream = self._stream, None
                with contextlib.suppress(Exception):
                    stream.abort()
            if self._conn is not None:
                conn, self._conn = self._conn, None
                with contextlib.suppress(Exception):
                    conn.close()
