"""Canonical VM lifecycle states — single source of truth.

State machine:
  REQUESTED -> PROVISIONING -> RUNNING <-> STOPPED -> DELETING -> (row removed)
                     `-> ERROR

The grouping tuples below drive resource accounting (see db.py): RAM/CPU is held
while a VM is requested/provisioning/running; disk while the overlay exists; and
a VM counts against the per-user VM cap for any of those live states.
"""
REQUESTED = "REQUESTED"
PROVISIONING = "PROVISIONING"
RUNNING = "RUNNING"
STOPPED = "STOPPED"
DELETING = "DELETING"
ERROR = "ERROR"

RAM_CPU_STATES = (REQUESTED, PROVISIONING, RUNNING)
DISK_STATES = (REQUESTED, PROVISIONING, RUNNING, STOPPED)
LIVE_STATES = (REQUESTED, PROVISIONING, RUNNING, STOPPED)
