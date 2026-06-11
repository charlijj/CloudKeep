"""Admin CLI to create / update CloudKeep users.

Usage (run on the host, in the controld venv):
  python seed_user.py alice
  python seed_user.py alice --admin --max-vms 4 --max-vcpus 8 \
                            --max-mem-mb 16384 --max-disk-gb 200

Password is read interactively (never on the command line). Re-running for an
existing username updates the password + quotas. Quotas default to the values
in config.py.
"""
import argparse
import getpass
import sys

import auth_core
from config import settings
from db import Database


def main() -> int:
    p = argparse.ArgumentParser(description="Create or update a CloudKeep user")
    p.add_argument("username")
    p.add_argument("--admin", action="store_true", help="grant admin role")
    p.add_argument("--max-vms", type=int, default=settings.DEFAULT_MAX_VMS)
    p.add_argument("--max-vcpus", type=int, default=settings.DEFAULT_MAX_VCPUS)
    p.add_argument("--max-mem-mb", type=int, default=settings.DEFAULT_MAX_MEM_MB)
    p.add_argument("--max-disk-gb", type=int, default=settings.DEFAULT_MAX_DISK_GB)
    args = p.parse_args()

    pw = getpass.getpass("password: ")
    if pw != getpass.getpass("confirm : "):
        print("passwords do not match", file=sys.stderr)
        return 1

    db = Database(settings.DB_PATH)
    pw_hash = auth_core.hash_password(pw)
    role = "admin" if args.admin else "user"
    existing = db.get_user(args.username)
    if existing:
        db._exec(
            "UPDATE users SET pw_hash=?, role=?, max_vms=?, max_vcpus=?, "
            "max_mem_mb=?, max_disk_gb=? WHERE username=?",
            (pw_hash, role, args.max_vms, args.max_vcpus, args.max_mem_mb,
             args.max_disk_gb, args.username),
        )
        print(f"updated user '{args.username}' ({role})")
    else:
        db.create_user(args.username, pw_hash, role, args.max_vms,
                       args.max_vcpus, args.max_mem_mb, args.max_disk_gb)
        print(f"created user '{args.username}' ({role})")
    print(f"  quota: {args.max_vms} VMs, {args.max_vcpus} vCPU, "
          f"{args.max_mem_mb} MB RAM, {args.max_disk_gb} GB disk")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
