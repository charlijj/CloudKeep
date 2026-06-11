"""Production entrypoint for cloudkeep-controld.

Latency-tuned uvicorn:
  * loop="uvloop"                 lower overhead for the WS<->VNC relay
  * ws_per_message_deflate=False  VNC payloads are already compressed

Run from this directory:  python run.py
Requires uvicorn[standard] (uvloop) — see ../requirements.txt.
"""
import uvicorn

from app import app
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
