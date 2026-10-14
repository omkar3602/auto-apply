"""Fires the scheduled auto-apply run: syncs every job source, then queues
the newest N still-undecided ("new") jobs for tsenta with no manual review
(matching a swipe-right/Apply click - see routes/jobs.py's apply_job route,
which does the same `status = "queued"` write).

Runs 4x/day - 11am, 2pm, 5pm, 8pm America/New_York, weekdays only - DST-safe
via zoneinfo.

Polls every POLL_INTERVAL_S and compares against wall-clock time, rather
than a single `time.sleep(seconds_until_next_fire)` (the shape
automation/reminders.py still uses). Confirmed in production twice
(2026-10-09, 2026-10-12) that a laptop going to sleep (lid closed, idle
sleep - caffeinate -s doesn't reliably prevent this, especially on battery)
can make a single long sleep() call never complete, silently dropping a
fire entirely rather than running it late. A short poll loop self-heals
regardless of how long or how many times the machine was actually asleep:
worst case a fire lands up to POLL_INTERVAL_S after the scheduled minute,
it never just vanishes. `_last_fired_slot` makes each (date, hour) slot
fire exactly once even though the hour stays "due" for the whole poll
window it's checked in.
"""

import threading
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_

import config as cfg
from db import db
from models import Job
from scrapers import SOURCES
from automation import push, schedule_state
from automation.tasks import run_sync_sync

STATE_KEY = "auto_apply_last_fired_slot"

SCHEDULE_TZ = ZoneInfo("America/New_York")
SCHEDULE_HOURS = (11, 14, 17, 20)
JOBS_PER_RUN = 20
POLL_INTERVAL_S = 60

# Sources like zapplyjobs/speedyapply scrape every row in their table with no
# keyword filter of their own (fine for browsing - the swipe tabs are the
# filter there), so with no human reviewing before applying, auto-apply needs
# its own relevance check or it'll happily queue things like "Transportation
# Specialist" or "Machine Shop Supervisor" right alongside SWE roles. Reuses
# the same keyword list Dice's own search is already scoped to.
#
# This has to be a SQL filter, not a Python filter over the newest N rows:
# a single source's sync can drop hundreds of same-timestamp "new" rows at
# once (zapplyjobs: 547), which would crowd every other source's actually-
# relevant jobs out of a recency-ordered top-N slice entirely.
RELEVANT_TITLE_KEYWORDS = cfg.DICE_SEARCH_KEYWORDS

_auto_apply_started = False


def _load_last_fired_slot():
    """Reads the persisted (date, hour) slot back from disk - a restart
    shouldn't forget a slot already fired earlier today (see
    automation/schedule_state.py)."""
    saved = schedule_state.load(STATE_KEY)
    if not saved:
        return None
    saved_date, saved_hour = saved
    return (date.fromisoformat(saved_date), saved_hour)


_last_fired_slot = _load_last_fired_slot()


def _relevant_title_filter():
    return or_(*(func.lower(Job.title).like(f"%{kw.lower()}%") for kw in RELEVANT_TITLE_KEYWORDS))


def _is_weekday(dt):
    return dt.weekday() < 5  # Mon=0 ... Fri=4, Sat=5, Sun=6


def _due_slot(now):
    """Returns the (date, hour) key for the schedule slot `now` currently
    falls in, or None if `now` isn't a fireable moment (weekday + one of
    SCHEDULE_HOURS)."""
    if not _is_weekday(now) or now.hour not in SCHEDULE_HOURS:
        return None
    return (now.date(), now.hour)


def _run_once(app):
    try:
        push.send_to_all(
            "Auto-apply running",
            "Syncing sources and queuing matching jobs for tsenta now.",
            url="/swipe",
        )
    except Exception:
        # A failed notification shouldn't block the actual run.
        app.logger.exception("[auto-apply schedule] start notification failed")

    for source in SOURCES:
        try:
            run_sync_sync(app, source)
        except Exception:
            # Already logged by run_sync_sync - one source failing to sync
            # shouldn't stop the others, or the apply step below.
            pass

    jobs = (
        Job.query.filter_by(status="new")
        .filter(_relevant_title_filter())
        .order_by(Job.created_at.desc())
        .limit(JOBS_PER_RUN)
        .all()
    )
    queued = 0
    for job in jobs:
        job.status = "queued"
        job.status_changed_at = datetime.utcnow()
        queued += 1
    if queued:
        db.session.commit()

    app.logger.info(
        "[auto-apply schedule] synced %d source(s), queued %d job(s)", len(SOURCES), queued
    )
    return queued


def start_auto_apply_schedule(app):
    """Starts the background thread. Safe to call more than once; only the
    first call actually starts it."""
    global _auto_apply_started
    if _auto_apply_started:
        return
    _auto_apply_started = True

    def worker():
        global _last_fired_slot
        with app.app_context():
            app.logger.info("[auto-apply schedule] background thread started")
            while True:
                try:
                    slot = _due_slot(datetime.now(SCHEDULE_TZ))
                    if slot is not None and slot != _last_fired_slot:
                        _last_fired_slot = slot
                        schedule_state.save(STATE_KEY, [slot[0].isoformat(), slot[1]])
                        _run_once(app)
                except Exception:
                    app.logger.exception("[auto-apply schedule] run failed")
                time.sleep(POLL_INTERVAL_S)

    threading.Thread(target=worker, daemon=True).start()
