"""SQLite persistence for cloudkeep-controld.

One file DB (WAL) on the host. The control plane is single-worker, so a single
shared connection guarded by a lock is sufficient and keeps transactions
simple. Calls are sub-millisecond on a tiny local DB; the few that matter for
correctness (allocation) run inside an asyncio lock at the manager layer.

State machine (vms.state):
  REQUESTED -> PROVISIONING -> RUNNING <-> STOPPED -> DELETING -> (row removed)
                     `-> ERROR
Resource accounting:
  RAM/CPU counted while a VM holds them: REQUESTED, PROVISIONING, RUNNING.
  Disk counted while the overlay exists:  + STOPPED.
  ERROR / DELETING hold nothing (rolled back).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

# States that count against the host RAM/CPU pool and the user's RAM/CPU quota.
RAM_CPU_STATES = ("REQUESTED", "PROVISIONING", "RUNNING")
# States that count against disk (overlay exists on the pool).
DISK_STATES = ("REQUESTED", "PROVISIONING", "RUNNING", "STOPPED")
# States that count against the per-user VM *count* quota.
LIVE_STATES = ("REQUESTED", "PROVISIONING", "RUNNING", "STOPPED")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT UNIQUE NOT NULL,
    pw_hash     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'user',
    max_vms     INTEGER NOT NULL,
    max_vcpus   INTEGER NOT NULL,
    max_mem_mb  INTEGER NOT NULL,
    max_disk_gb INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vms (
    id         INTEGER PRIMARY KEY,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    name       TEXT UNIQUE NOT NULL,          -- libvirt domain name: vm-<id>
    label      TEXT NOT NULL,                 -- user-facing display name
    vcpus      INTEGER NOT NULL,
    mem_mb     INTEGER NOT NULL,
    disk_gb    INTEGER NOT NULL,
    state      TEXT NOT NULL,
    mac        TEXT,
    ip_addr    TEXT,
    vnc_port   INTEGER NOT NULL DEFAULT 5901,
    error_msg  TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pcie (
    id             INTEGER PRIMARY KEY,
    pci_address    TEXT UNIQUE NOT NULL,
    label          TEXT NOT NULL,
    assigned_vm_id INTEGER REFERENCES vms(id)
);
CREATE TABLE IF NOT EXISTS audit (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    user_id INTEGER,
    action  TEXT NOT NULL,
    detail  TEXT
);
"""


class Database:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- low-level helpers -------------------------------------------------
    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.lastrowid

    # -- users -------------------------------------------------------------
    def get_user(self, username: str) -> Optional[dict]:
        row = self._one("SELECT * FROM users WHERE username=?", (username,))
        return dict(row) if row else None

    def create_user(self, username: str, pw_hash: str, role: str,
                    max_vms: int, max_vcpus: int, max_mem_mb: int,
                    max_disk_gb: int) -> int:
        return self._exec(
            "INSERT INTO users(username,pw_hash,role,max_vms,max_vcpus,"
            "max_mem_mb,max_disk_gb,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (username, pw_hash, role, max_vms, max_vcpus, max_mem_mb,
             max_disk_gb, _now()),
        )

    def update_user(self, username: str, pw_hash: str, role: str,
                    max_vms: int, max_vcpus: int, max_mem_mb: int,
                    max_disk_gb: int) -> None:
        self._exec(
            "UPDATE users SET pw_hash=?, role=?, max_vms=?, max_vcpus=?, "
            "max_mem_mb=?, max_disk_gb=? WHERE username=?",
            (pw_hash, role, max_vms, max_vcpus, max_mem_mb, max_disk_gb,
             username),
        )

    # -- vms ---------------------------------------------------------------
    def create_vm(self, owner_id: int, label: str, vcpus: int, mem_mb: int,
                  disk_gb: int, mac: str, ip_addr: str, vnc_port: int) -> int:
        # Two-step: insert to get the id, then set name=vm-<id> (domain name).
        vm_id = self._exec(
            "INSERT INTO vms(owner_id,name,label,vcpus,mem_mb,disk_gb,state,"
            "mac,ip_addr,vnc_port,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (owner_id, "", label, vcpus, mem_mb, disk_gb, "REQUESTED",
             mac, ip_addr, vnc_port, _now()),
        )
        self._exec("UPDATE vms SET name=? WHERE id=?", (f"vm-{vm_id}", vm_id))
        return vm_id

    def get_vm(self, vm_id: int) -> Optional[dict]:
        row = self._one("SELECT * FROM vms WHERE id=?", (vm_id,))
        return dict(row) if row else None

    def list_vms(self, owner_id: Optional[int] = None) -> list[dict]:
        if owner_id is None:
            rows = self._q("SELECT * FROM vms ORDER BY id")
        else:
            rows = self._q("SELECT * FROM vms WHERE owner_id=? ORDER BY id",
                           (owner_id,))
        return [dict(r) for r in rows]

    def set_state(self, vm_id: int, state: str, error_msg: str | None = None) -> None:
        self._exec("UPDATE vms SET state=?, error_msg=? WHERE id=?",
                   (state, error_msg, vm_id))

    def delete_vm(self, vm_id: int) -> None:
        self._exec("DELETE FROM vms WHERE id=?", (vm_id,))

    def used_ips(self) -> set[str]:
        return {r["ip_addr"] for r in self._q(
            "SELECT ip_addr FROM vms WHERE ip_addr IS NOT NULL")}

    def used_macs(self) -> set[str]:
        return {r["mac"] for r in self._q(
            "SELECT mac FROM vms WHERE mac IS NOT NULL")}

    # -- accounting --------------------------------------------------------
    def host_allocation(self) -> dict:
        """Sum of resources currently held by VMs across all users."""
        return self._alloc_where("1=1", ())

    def user_allocation(self, owner_id: int) -> dict:
        return self._alloc_where("owner_id=?", (owner_id,))

    def _alloc_where(self, clause: str, args: tuple) -> dict:
        ram_states = _in_list(RAM_CPU_STATES)
        disk_states = _in_list(DISK_STATES)
        live_states = _in_list(LIVE_STATES)
        row = self._one(
            f"SELECT "
            f" COALESCE(SUM(CASE WHEN state IN {ram_states} THEN vcpus END),0) vcpus,"
            f" COALESCE(SUM(CASE WHEN state IN {ram_states} THEN mem_mb END),0) mem_mb,"
            f" COALESCE(SUM(CASE WHEN state IN {disk_states} THEN disk_gb END),0) disk_gb,"
            f" COALESCE(SUM(CASE WHEN state IN {live_states} THEN 1 END),0) vms"
            f" FROM vms WHERE {clause}",
            args,
        )
        return {"vcpus": row["vcpus"], "mem_mb": row["mem_mb"],
                "disk_gb": row["disk_gb"], "vms": row["vms"]}

    # -- audit -------------------------------------------------------------
    def audit(self, user_id: Optional[int], action: str, detail: str = "") -> None:
        self._exec("INSERT INTO audit(ts,user_id,action,detail) VALUES(?,?,?,?)",
                   (_now(), user_id, action, detail))


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _in_list(states: tuple[str, ...]) -> str:
    # States are module constants (never user input) -> safe to inline.
    return "(" + ",".join(f"'{s}'" for s in states) + ")"
