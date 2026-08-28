"""Fires the daily "time to apply" push reminder at a fixed local time.

A single background thread sleeps until the next 11:00 AM America/New_York,
sends the push to every subscribed device, then loops for the following
day. `zoneinfo` handles the DST transition automatically, so this stays
11:00 AM local time year-round without any special-casing.
"""

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from automation import push

REMINDER_TZ = ZoneInfo("America/New_York")
REMINDER_HOUR = 11
REMINDER_MINUTE = 0

_reminder_started = False


def _seconds_until_next_fire():
    now = datetime.now(REMINDER_TZ)
    fire = now.replace(hour=REMINDER_HOUR, minute=REMINDER_MINUTE, second=0, microsecond=0)
    if fire <= now:
        fire += timedelta(days=1)
    return (fire - now).total_seconds()


def start_daily_reminder(app):
    """Starts the background thread. Safe to call more than once; only the
    first call actually starts it."""
    global _reminder_started
    if _reminder_started:
        return
    _reminder_started = True

    def worker():
        with app.app_context():
            while True:
                time.sleep(_seconds_until_next_fire())
                try:
                    sent, failed, pruned = push.send_to_all(
                        "Time to apply",
                        "New listings are waiting in Auto Apply - go swipe through today's batch.",
                        url="/swipe",
                    )
                    app.logger.info(
                        "[daily reminder] sent=%s failed=%s pruned=%s", sent, failed, pruned
                    )
                except Exception:
                    app.logger.exception("[daily reminder] failed")

    threading.Thread(target=worker, daemon=True).start()
