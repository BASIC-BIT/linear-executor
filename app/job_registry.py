"""Thread-safe registry of in-flight Claude subprocesses keyed by ticket id.

When orchestrate_start / orchestrate_proxy spawn a Claude subprocess, they
register the resulting Popen so that an out-of-band cancel webhook can find
and terminate it. Registry is in-memory only; if the executor process
restarts, in-flight processes become orphans (they keep running until they
finish but cancellation no longer reaches them). See TES-596 ADR Option A.

Design notes:

* Per-ticket single Popen — if the same ticket somehow has two parallel
  runs, only the most recent registration is reachable for cancel. In
  practice the queue prevents duplicate runs (status ``running``).
* Popen.terminate sends SIGTERM to the process group when the Popen was
  created with ``start_new_session=True``. After a short grace period the
  caller should send SIGKILL via Popen.kill if needed.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from typing import Dict, Optional


logger = logging.getLogger("linear-executor")


_lock = threading.Lock()
_active: Dict[str, subprocess.Popen] = {}


def register(ticket_id: str, popen: subprocess.Popen) -> None:
    """Record a running Popen so cancel can find it later."""
    with _lock:
        _active[ticket_id] = popen
    logger.info("job-registry — registered ticket=%s pid=%d", ticket_id, popen.pid)


def unregister(ticket_id: str) -> None:
    """Remove a finished/cancelled run from the registry. No-op if absent."""
    with _lock:
        popped = _active.pop(ticket_id, None)
    if popped is not None:
        logger.info("job-registry — unregistered ticket=%s", ticket_id)


def get(ticket_id: str) -> Optional[subprocess.Popen]:
    """Return the active Popen for ticket_id, or None if no active run."""
    with _lock:
        return _active.get(ticket_id)


def active_tickets() -> list[str]:
    """Snapshot of currently registered ticket ids (for diagnostics)."""
    with _lock:
        return list(_active.keys())


def clear() -> None:
    """Reset the registry (for tests)."""
    with _lock:
        _active.clear()
