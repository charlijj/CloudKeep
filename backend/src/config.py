"""Centralised settings for cloudkeep-controld (the v2 control plane).

Runs on the KVM host. Single source of truth; secrets come from .env beside
this module (JWT_SECRET fails closed). Host *totals* (CPU/RAM/disk) are read
live from libvirt at runtime — only the reserves and policy live here.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # --- Control-plane listen address (WireGuard interface only) ---
    LISTEN_HOST: str = "10.10.10.2"
    LISTEN_PORT: int = 8000

    # --- Auth ---
    JWT_SECRET: str                     # no default -> fail closed
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 60
    WS_TOKEN_EXPIRY_SECONDS: int = 30
    ALLOWED_ORIGIN: str = "https://cloudkeep-auth.duckdns.org"
    AUTH_RATE_LIMIT: str = "5/minute"
    PROVISION_RATE_LIMIT: str = "5/hour"

    # --- Live serial console (boot-log streaming) ---
    # Each open stream holds one libvirt connection + one reader thread, so cap
    # how many run at once to bound resource use from the (authenticated)
    # console surface. The in-memory byte queue is bounded too: a guest spamming
    # its serial port drops oldest rather than growing controld's memory.
    CONSOLE_MAX_STREAMS: int = 8
    CONSOLE_QUEUE_MAX: int = 1024

    # --- Self-service account requests (admin-approved) ---
    # The public landing form POSTs here. A request is INERT: it only enqueues a
    # pending row — it creates no user and grants no access. Only the host-side
    # review_requests.py CLI (run by an admin) turns a request into a real user.
    # Fail-closed gate: an invite code is REQUIRED, and if no codes are
    # configured the endpoint rejects everything (signups closed by default).
    ACCOUNT_REQUESTS_ENABLED: bool = True
    SIGNUP_INVITE_CODES: str = ""        # comma-separated; empty => signups closed
    SIGNUP_RATE_LIMIT: str = "5/hour"    # per-IP, at controld (edge throttles too)
    MAX_PENDING_REQUESTS: int = 10       # global cap on the pending queue
    MAX_PENDING_PER_IP: int = 1          # pending requests one IP may hold at once

    # --- Email notifications (GROUNDWORK — disabled) ---
    # Fully scaffolded but inert: with SMTP_ENABLED=false (default) notify.py only
    # logs what it WOULD send and opens no network connection. Set SMTP_ENABLED
    # plus the SMTP_* values in .env to turn it on later — no code change needed.
    SMTP_ENABLED: bool = False
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM: str = ""                  # e.g. "CloudKeep <no-reply@example.com>"
    SMTP_USE_TLS: bool = True            # STARTTLS on SMTP_PORT
    PORTAL_URL: str = ""                 # email link base; defaults to ALLOWED_ORIGIN

    # --- Persistence ---
    DB_PATH: str = "/var/lib/cloudkeep/db/cloudkeep.db"

    # --- Virtualisation ---
    # Set LIBVIRT_URI=fake to exercise the control plane (API/DB/quotas) with
    # the in-memory FakeProvisioner, no libvirt/KVM required.
    LIBVIRT_URI: str = "qemu:///system"
    LIBVIRT_NETWORK: str = "ckbr0"
    STORAGE_POOL: str = "cloudkeep"
    IMAGE_DIR: str = "/var/lib/cloudkeep/images"
    GOLDEN_IMAGE: str = "/var/lib/cloudkeep/images/golden-v1.qcow2"
    OS_VARIANT: str = "ubuntu24.04"
    PROVISION_TIMEOUT_S: int = 300      # wait for VNC ready before ERROR; a clone's
                                        # first boot (disk resize + full desktop) is slow

    # --- Guest network (isolated; VMs get a deterministic lease here) ---
    GUEST_NET_PREFIX: str = "10.20.0"   # /24
    GUEST_IP_START: int = 50            # .50 .. .250 assignable
    GUEST_IP_END: int = 250
    GUEST_VNC_PORT: int = 5901          # in-guest TigerVNC; bridge target

    # --- Host reserves (never handed to guests) ---
    RESERVE_VCPUS: int = 2
    RESERVE_MEM_MB: int = 4096
    RESERVE_DISK_GB: int = 50

    # --- Per-user quota defaults (seed_user.py may override per user) ---
    DEFAULT_MAX_VMS: int = 2
    DEFAULT_MAX_VCPUS: int = 4
    DEFAULT_MAX_MEM_MB: int = 8192
    DEFAULT_MAX_DISK_GB: int = 100

    # --- Builder sizing bounds (UI caps to min(quota, host-free) within these) ---
    VM_MIN_VCPUS: int = 1
    VM_MAX_VCPUS: int = 8
    VM_MIN_MEM_MB: int = 2048           # a browser desktop on software rendering
                                        # needs >1 GB; 2 GB is the usable floor
    VM_MAX_MEM_MB: int = 16384
    VM_MEM_STEP_MB: int = 1024
    VM_MIN_DISK_GB: int = 16
    VM_MAX_DISK_GB: int = 200

    @property
    def use_fake(self) -> bool:
        return self.LIBVIRT_URI.strip().lower() == "fake"

    @property
    def invite_codes(self) -> set[str]:
        """Parsed set of valid invite codes (empty => signups closed)."""
        return {c.strip() for c in self.SIGNUP_INVITE_CODES.split(",") if c.strip()}

    @property
    def portal_url(self) -> str:
        """Base URL for links in notifications; falls back to the edge origin."""
        return (self.PORTAL_URL or self.ALLOWED_ORIGIN).rstrip("/")


settings = Settings()
