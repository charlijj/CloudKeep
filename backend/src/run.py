"""Production entrypoint for the CloudKeep gateway.

Runs uvicorn with latency-tuned settings so the byte relay stays tight:

  * loop="uvloop"
        Lower per-operation overhead than the stock asyncio loop for this
        I/O-bound TCP<->WebSocket proxy.

  * ws_per_message_deflate=False
        VNC payloads are already compressed (Tight/JPEG/zlib). Deflating them
        a second time only burns CPU on both ends and adds a compression stage
        to every frame — pure latency tax for ~zero size gain.

Run from this directory (so `auth`/`config` import cleanly):

    python run.py

Requires uvicorn >= 0.18 (for ws_per_message_deflate) with the uvloop extra
(`uvicorn[standard]`); see ../requirements.txt.
"""
import uvicorn

from auth import app
from config import settings

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.LISTEN_HOST,
        port=settings.LISTEN_PORT,
        loop="uvloop",
        ws_per_message_deflate=False,
        log_level="info",
    )
