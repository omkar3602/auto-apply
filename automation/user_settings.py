"""Tiny on-disk JSON store for global user preferences (keyboard mode,
which sources feed swipe mode) - same key-value-JSON-file shape as
automation/schedule_state.py, kept in a separate file since these aren't
scheduler state.

Read and written through routes/settings.py, which every page's gear icon
talks to (templates/base.html) - a deliberate server-side choice over
localStorage: this app is used from more than one device (laptop,
phone over tailscale), and localStorage is per-browser, so a preference
set on one device would never show up on the other.
"""

import json
import os

import config as cfg

_SETTINGS_FILE = os.path.join(cfg.INSTANCE_DIR, "user_settings.json")

# swipe_sources: None means "no filter - every source" (the original,
# unfiltered behavior); a list (including an empty one, chosen on purpose)
# filters to just those sources.
DEFAULTS = {
    "keyboard_mode": False,
    "swipe_sources": None,
}


def load():
    try:
        with open(_SETTINGS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    return {**DEFAULTS, **data}


def save(partial):
    """Merges `partial` into the persisted settings and returns the full,
    merged result."""
    current = load()
    current.update(partial)
    with open(_SETTINGS_FILE, "w") as f:
        json.dump(current, f)
    return current
