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
    ALLOWED_ORIGIN: str = "https://cloudkeep.duckdns.org"
    AUTH_RATE_LIMIT: str = "5/minute"
    PROVISION_RATE_LIMIT: str = "5/hour"

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
    VM_MIN_MEM_MB: int = 1024
    VM_MAX_MEM_MB: int = 16384
    VM_MEM_STEP_MB: int = 1024
    VM_MIN_DISK_GB: int = 16
    VM_MAX_DISK_GB: int = 200

    @property
    def use_fake(self) -> bool:
        return self.LIBVIRT_URI.strip().lower() == "fake"


settings = Settings()
