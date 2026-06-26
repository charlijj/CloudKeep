"""Admin CLI to review self-service account requests (run on the host).

Requests submitted from the public landing form land in a pending queue — they
are INERT and grant nothing. This tool is the ONLY path that turns one into a
real user: an admin lists pending requests, then approves (setting a password +
quotas, which creates the user) or denies them. Same trust level and host-only
posture as seed_user.py; it shares the exact create-user path (useradmin).

Usage (in the controld venv, as the service user):
  python review_requests.py list                 # pending (add --all for every status)
  python review_requests.py show 3
  python review_requests.py approve 3 [--admin --max-vms 4 --max-vcpus 8 ...]
  python review_requests.py deny 3 --reason "unrecognised requester"
  python review_requests.py purge --days 30      # drop old decided requests
"""
from __future__ import annotations

import argparse
import datetime
import getpass
import sys

from config import settings
from db import Database
from notify import notify_account_approved
from useradmin import upsert_user
from validation import clean_username


def _fmt(r: dict) -> str:
    return (f"#{r['id']:<4} {r['created_at']}  {r['username']:<20} "
            f"{r['email']:<28} {r['first_name']} {r['last_name']}  "
            f"[{r['status']}]  ip={r['src_ip'] or '?'}")


def cmd_list(db: Database, args) -> int:
    reqs = db.list_account_requests(None if args.all else "pending")
    if not reqs:
        print("no requests" if args.all else "no pending requests")
        return 0
    for r in reqs:
        print(_fmt(r))
    return 0


def cmd_show(db: Database, args) -> int:
    r = db.get_account_request(args.id)
    if not r:
        print(f"no request #{args.id}", file=sys.stderr)
        return 1
    for k in ("id", "username", "email", "first_name", "last_name", "status",
              "src_ip", "created_at", "decided_at", "decided_by", "note"):
        print(f"  {k:<11}: {r.get(k)}")
    return 0


def cmd_approve(db: Database, args) -> int:
    r = db.get_account_request(args.id)
    if not r:
        print(f"no request #{args.id}", file=sys.stderr)
        return 1
    if r["status"] != "pending":
        print(f"request #{args.id} is already {r['status']}", file=sys.stderr)
        return 1

    # Re-validate the stored handle (defense in depth) and refuse to clobber an
    # existing user — the admin should deny + ask the requester to pick another.
    try:
        username = clean_username(r["username"])
    except ValueError as e:
        print(f"stored username is invalid ({e}); deny this request instead",
              file=sys.stderr)
        return 1
    if db.get_user(username):
        print(f"a user named '{username}' already exists; deny this request "
              f"(or have them request a different name)", file=sys.stderr)
        return 1

    print(f"Approving #{r['id']}: {username} <{r['email']}> "
          f"({r['first_name']} {r['last_name']})")
    pw = getpass.getpass("set password: ")
    if pw != getpass.getpass("confirm     : "):
        print("passwords do not match", file=sys.stderr)
        return 1

    role = "admin" if args.admin else "user"
    upsert_user(db, settings, username=username, password=pw, role=role,
                max_vms=args.max_vms, max_vcpus=args.max_vcpus,
                max_mem_mb=args.max_mem_mb, max_disk_gb=args.max_disk_gb,
                email=r["email"], first_name=r["first_name"],
                last_name=r["last_name"])
    by = getpass.getuser()
    db.set_request_status(r["id"], "approved", note=args.note, decided_by=by)
    user = db.get_user(username)
    db.audit(user["id"] if user else None, "account.approve",
             f"req#{r['id']} {username} by {by}")
    notify_account_approved(settings, email=r["email"], username=username,
                            first_name=r["first_name"])
    print(f"created user '{username}' ({role}); quota {args.max_vms} VMs / "
          f"{args.max_vcpus} vCPU / {args.max_mem_mb} MB / {args.max_disk_gb} GB")
    print("Deliver the password to the user out-of-band "
          "(email notifications are not enabled).")
    return 0


def cmd_deny(db: Database, args) -> int:
    r = db.get_account_request(args.id)
    if not r:
        print(f"no request #{args.id}", file=sys.stderr)
        return 1
    if r["status"] != "pending":
        print(f"request #{args.id} is already {r['status']}", file=sys.stderr)
        return 1
    by = getpass.getuser()
    db.set_request_status(r["id"], "denied", note=args.reason, decided_by=by)
    db.audit(None, "account.deny",
             f"req#{r['id']} {r['username']} by {by}: {args.reason or ''}")
    print(f"denied request #{r['id']} ({r['username']})")
    return 0


def cmd_purge(db: Database, args) -> int:
    cutoff = (datetime.datetime.utcnow()
              - datetime.timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = db.purge_account_requests(cutoff, only_decided=not args.include_pending)
    scope = "requests" if args.include_pending else "decided requests"
    print(f"purged {n} {scope} older than {args.days} days")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Review CloudKeep account requests")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list requests (pending by default)")
    pl.add_argument("--all", action="store_true", help="include decided ones")
    pl.set_defaults(func=cmd_list)

    ps = sub.add_parser("show", help="show one request in full")
    ps.add_argument("id", type=int)
    ps.set_defaults(func=cmd_show)

    pa = sub.add_parser("approve", help="approve a request -> create the user")
    pa.add_argument("id", type=int)
    pa.add_argument("--admin", action="store_true", help="grant admin role")
    pa.add_argument("--max-vms", type=int, default=settings.DEFAULT_MAX_VMS)
    pa.add_argument("--max-vcpus", type=int, default=settings.DEFAULT_MAX_VCPUS)
    pa.add_argument("--max-mem-mb", type=int, default=settings.DEFAULT_MAX_MEM_MB)
    pa.add_argument("--max-disk-gb", type=int, default=settings.DEFAULT_MAX_DISK_GB)
    pa.add_argument("--note", default=None, help="optional approval note")
    pa.set_defaults(func=cmd_approve)

    pd = sub.add_parser("deny", help="deny a request")
    pd.add_argument("id", type=int)
    pd.add_argument("--reason", default=None, help="optional denial reason")
    pd.set_defaults(func=cmd_deny)

    pp = sub.add_parser("purge", help="delete old requests")
    pp.add_argument("--days", type=int, default=30)
    pp.add_argument("--include-pending", action="store_true",
                    help="also drop stale pending requests (default: decided only)")
    pp.set_defaults(func=cmd_purge)

    args = p.parse_args()
    db = Database(settings.DB_PATH)
    return args.func(db, args)


if __name__ == "__main__":
    raise SystemExit(main())
