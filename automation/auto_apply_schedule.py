"""When the auto-apply pass runs: hourly, 9am-8pm America/New_York, weekdays only.

What a pass does lives in automation/auto_apply_run.py; this file is only the clock.

Polls every POLL_INTERVAL_S and compares against wall-clock time, rather than a single
`time.sleep(seconds_until_next_fire)` (the shape automation/reminders.py still uses).
Confirmed in production twice (2026-10-09, 2026-10-12) that a laptop going to sleep can
make one long sleep() never complete, silently dropping a fire. A short poll loop
self-heals: a slot missed while the machine slept runs late (the catch-up), it never
just vanishes. `_last_fired_slot` - persisted in automation/schedule_state.py so a
restart doesn't forget it - makes each (date, hour) slot fire exactly once.

Two passes can't overlap: the run holds a lock (thread + file). If a slot comes due
while the previous pass is still going, the slot is left unfired and retried on the
next poll rather than lost.
"""

import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from automation import auto_apply_run, schedule_state

STATE_KEY = "auto_apply_last_fired_slot"

SCHEDULE_TZ = ZoneInfo("America/New_York")
SCHEDULE_HOURS = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
POLL_INTERVAL_S = 60

# zapplyjobs and speedyapply were dropped from auto-apply on 2026-11-25 (still browsable in the
# app). Their scrapers are untouched; only the scheduler stopped using them.
AUTO_APPLY_SOURCES = auto_apply_run.SCRAPER_SOURCES

_auto_apply_started = False


def _load_last_fired_slot():
    saved = schedule_state.load(STATE_KEY)
    if not saved:
        return None
    saved_date, saved_hour = saved
    return (date.fromisoformat(saved_date), saved_hour)


_last_fired_slot = _load_last_fired_slot()


def _is_weekday(dt):
    return dt.weekday() < 5  # Mon=0 ... Fri=4


def _due_slot(now):
    """The (date, hour) slot `now` falls in, or None outside weekday 9:00-20:59."""
    if not _is_weekday(now) or now.hour not in SCHEDULE_HOURS:
        return None
    return (now.date(), now.hour)


def _run_once(app):
    return auto_apply_run.run_once(app, trigger="schedule")


def tick(app, now=None):
    """One poll. Returns the run if one fired, else None. Split out so tests drive it directly."""
    global _last_fired_slot
    slot = _due_slot(now or datetime.now(SCHEDULE_TZ))
    if slot is None or slot == _last_fired_slot:
        return None
    previous = _last_fired_slot
    _last_fired_slot = slot
    schedule_state.save(STATE_KEY, [slot[0].isoformat(), slot[1]])
    run = None
    try:
        run = _run_once(app)
    finally:
        if run is None:                                  # busy (or crashed before starting): try again next poll
            _last_fired_slot = previous
            schedule_state.save(STATE_KEY, [previous[0].isoformat(), previous[1]] if previous else None)
    return run


def start_auto_apply_schedule(app):
    """Starts the background thread. Safe to call more than once."""
    global _auto_apply_started
    if _auto_apply_started:
        return
    _auto_apply_started = True

    def worker():
        with app.app_context():
            app.logger.info("[auto-apply schedule] background thread started")
            while True:
                try:
                    tick(app)
                except Exception:
                    app.logger.exception("[auto-apply schedule] run failed")
                time.sleep(POLL_INTERVAL_S)

    threading.Thread(target=worker, daemon=True).start()
