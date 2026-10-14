"""Fires the daily "time to apply" push reminder at a fixed local time.

Polls every POLL_INTERVAL_S and compares against wall-clock time, rather
than a single `time.sleep(seconds_until_next_fire)`. automation/
auto_apply_schedule.py used that shape originally and, confirmed in
production twice (2026-10-09, 2026-10-12), a laptop going to sleep (lid
closed, idle sleep - caffeinate -s doesn't reliably prevent this,
especially on battery) can make a single long sleep() call never complete,
silently dropping the day's reminder entirely rather than sending it late.
A short poll loop self-heals regardless of how long or how many times the
machine was actually asleep: worst case the reminder lands a bit after
11:00 AM, it never just vanishes. `_last_fired_date` makes it fire at most
once per day even though the trigger stays "due" for the rest of that
minute/hour it's checked in.
"""

import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from automation import push, schedule_state

REMINDER_TZ = ZoneInfo("America/New_York")
REMINDER_HOUR = 11
REMINDER_MINUTE = 0
POLL_INTERVAL_S = 60
STATE_KEY = "reminder_last_fired_date"

_reminder_started = False


def _load_last_fired_date():
    """Reads the persisted date back from disk - a restart shouldn't forget
    today's reminder already went out (see automation/schedule_state.py)."""
    saved = schedule_state.load(STATE_KEY)
    return date.fromisoformat(saved) if saved else None


_last_fired_date = _load_last_fired_date()


def _is_due(now):
    if (now.hour, now.minute) < (REMINDER_HOUR, REMINDER_MINUTE):
        return False
    return now.date() != _last_fired_date


def start_daily_reminder(app):
    """Starts the background thread. Safe to call more than once; only the
    first call actually starts it."""
    global _reminder_started
    if _reminder_started:
        return
    _reminder_started = True

    def worker():
        global _last_fired_date
        with app.app_context():
            app.logger.info("[daily reminder] background thread started")
            while True:
                try:
                    now = datetime.now(REMINDER_TZ)
                    if _is_due(now):
                        _last_fired_date = now.date()
                        schedule_state.save(STATE_KEY, _last_fired_date.isoformat())
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
                time.sleep(POLL_INTERVAL_S)

    threading.Thread(target=worker, daemon=True).start()
