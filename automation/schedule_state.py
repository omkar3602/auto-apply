"""Tiny on-disk key-value store for background scheduler "already fired"
markers (automation/auto_apply_schedule.py, automation/reminders.py).

Both schedulers poll against wall-clock time and dedupe within a process
by keeping an in-memory "last fired" marker - that fixed the original bug
(a laptop sleep silently skipping a fire), but a plain process restart
*after* a slot has already fired today wipes that memory and re-fires it.
Confirmed in production (2026-10-12): three duplicate daily reminder
pushes from three routine restarts in one afternoon. Persisting the
marker to disk here means a restart re-reads what actually already
happened today instead of assuming nothing has.
"""

import json
import os

import config as cfg

_STATE_FILE = os.path.join(cfg.INSTANCE_DIR, "schedule_state.json")


def _read_all():
    try:
        with open(_STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load(key):
    """Returns the persisted value for `key`, or None if never set."""
    return _read_all().get(key)


def save(key, value):
    """Persists `value` (must be JSON-serializable) under `key`."""
    data = _read_all()
    data[key] = value
    with open(_STATE_FILE, "w") as f:
        json.dump(data, f)
