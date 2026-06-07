"""Centralised settings for the CloudKeep gateway.

Single source of truth for configuration. Secrets are loaded from the
environment (.env); nothing sensitive is hardcoded here.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env next to this file, so the app works regardless of which
# directory it is launched from or where the project is installed.
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Runtime configuration, loaded from a .env beside this module."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # VNC backend — always loopback on this host.
    VNC_HOST: str = "127.0.0.1"
    VNC_PORT: int = 5901

    # FastAPI listen address — WireGuard interface only, never 0.0.0.0.
    LISTEN_HOST: str = "10.10.10.2"
    LISTEN_PORT: int = 8000

    # JWT signing. JWT_SECRET has no default on purpose: the app must
    # refuse to start if it is missing from the environment (fail closed).
    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 60

    # One-time WebSocket session tokens.
    WS_TOKEN_EXPIRY_SECONDS: int = 30

    # CORS / origin allowlist (single origin, no wildcard).
    ALLOWED_ORIGIN: str = "https://cloudkeep.duckdns.org"

    # Per-IP rate limit on the login endpoint.
    AUTH_RATE_LIMIT: str = "5/minute"


settings = Settings()

# POC user store. Kept separate from Settings so it can be swapped for a
# database lookup in v2 without touching the settings class.
#
# Generate a hash, then paste it below:
#   python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
#
# Placeholder below is NOT a valid hash — replace it before first login.
USERS: dict[str, str] = {
    "command_user": "$2b$12$xhwV60T5b64awrYRUqAB4uSAu6N5IiS7jplWUm/jfBVe1tw2ZNDlC",
}
