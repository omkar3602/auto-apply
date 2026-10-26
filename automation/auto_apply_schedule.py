"""Fires the scheduled auto-apply run: syncs jobright and simplify (see
AUTO_APPLY_SOURCES), then queues the newest N still-undecided ("new"),
relevant, under-24h-old jobs for tsenta with no manual review (matching a
swipe-right/Apply click - see routes/jobs.py's apply_job route, which does
the same `status = "queued"` write).

Runs hourly, 9am-8pm America/New_York, weekdays only - DST-safe
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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_

import config as cfg
from db import db
from models import Job
from automation import schedule_state
from automation.tasks import run_sync_sync
from automation.tsenta import resolve_apply_link

STATE_KEY = "auto_apply_last_fired_slot"

SCHEDULE_TZ = ZoneInfo("America/New_York")
SCHEDULE_HOURS = (9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20)
JOBS_PER_RUN = 40
POLL_INTERVAL_S = 60

# Dice was tried and removed entirely (2026-10-26) - its real apply success
# rate never recovered from 0/154, even after building and fixing a
# logged-in redirect resolver: only ~1 in 6 postings had an external apply
# link at all (the rest apply inside Dice's own authenticated "Easy Apply"
# flow, unreachable by tsenta regardless of login), and the one external
# link found in testing wasn't a platform tsenta could submit through
# either. No longer a source anywhere in this app, not just auto-apply.
#
# zapplyjobs added 2026-10-19: jobright+simplify alone averaged only ~24
# qualifying postings/day against this schedule's demand (12 runs x
# JOBS_PER_RUN), while zapplyjobs alone averages ~48/day of its own (its
# scraper already applies the same relevant-title filter at scrape time -
# see scrapers/zapplyjobs.py), with ~74% real apply success once its
# zapply.jobs redirect links resolve to the real employer URL (see
# automation/tsenta.py's _resolve_zapply_redirect) - not as clean as
# jobright/simplify's 90%+, but solidly usable.
#
# speedyapply added 2026-10-26: only ~6.5 qualifying postings/day, the
# smallest contribution of any source, but the best reliability track
# record of all of them - 90/90 real manual applies succeeded, zero
# failures ever, because its listings link straight to well-known ATS
# platforms (Ashby, Greenhouse, Lever, Workday, Workable, SmartRecruiters)
# with no redirect/resolution step the way zapplyjobs needs.
AUTO_APPLY_SOURCES = ("jobright", "simplify", "zapplyjobs", "speedyapply")

# Retightened to 24h (2026-10-19): the site-wide scraper cutoff
# (scrapers/common.MAX_JOB_AGE_SECONDS) dropped from 3 days to 1 day the
# same day, so nothing in the database is ever older than 24h to begin
# with any more - the 48h widening from 2026-10-16 (see below) had become
# a pure no-op, not an actual looser window. Kept at 24h rather than
# dropped entirely so the intent stays explicit/self-documenting here.
#
# Original 2026-10-16 note, for context: once both the relevance and
# supported-ATS filters are applied too, a 24h window could come up with
# zero eligible candidates in an unlucky run (confirmed same day - a real
# 2pm run found 7 relevant jobs and all 7 happened to be on unsupported
# platforms). If that recurs now that the site-wide cutoff is also 24h,
# the fix has to happen in scrapers/common.MAX_JOB_AGE_SECONDS instead -
# this constant alone can't widen the pool past what the site shows at all.
RECENT_WINDOW_HOURS = 24

# Switched from an allowlist to a blocklist (2026-10-16): the allowlist
# only let through domains already proven to work, which also silently
# excluded untested-but-probably-fine company career portals (Nike,
# Amazon, Capgemini, Qualtrics, Intuit, ...) tsenta has simply never been
# tried against yet - confirmed same day, a real run found 19 relevant
# candidates and the allowlist rejected all 19. tsenta advertises handling
# 50,000+ career pages generally, not just named ATSes, so default to
# trying an unknown domain and letting a real failure be caught honestly
# (apply_failed) rather than preemptively assuming it won't work.
#
# Each entry here has strong real-outcome evidence, not a single fluke:
# www.linkedin.com and lifeattiktok.com are structurally not the employer's
# own application page (LinkedIn's posting aggregator, TikTok's own careers
# search) rather than an ATS that sometimes has an off day;
# apply.careers.microsoft.com was directly tested here and confirmed to
# reject tsenta's auto-detection. One-off single-attempt failures
# (corningjobs.corning.com, careers.ey.com, etc.) are NOT included - one bad
# run isn't enough evidence to permanently block a domain. (www.dice.com was
# here too until Dice was removed as a source entirely, 2026-10-26 - no
# longer needed once nothing ever produces a dice.com job_url.)
BLOCKED_URL_PATTERNS = (
    "%linkedin.com/%",
    "%lifeattiktok.com/%",
    "%apply.careers.microsoft.com/%",
)

# Confirmed 2026-10-26 (a week of real auto-apply data, see the research
# writeup in conversation): zapplyjobs' own "Apply" links are often already
# dead at the source, not stale from our 24h window - every failure
# applied within the same hour it was scraped, and 94% of zapplyjobs
# failures that week resolved to this exact generic listing page instead
# of a real posting. That's 31% of ALL auto-apply attempts that week,
# across every source. _next_candidate below resolves each zapplyjobs
# candidate's redirect *before* queuing it and skips this exact dead end,
# pulling the next candidate instead - so a doomed slot never consumes one
# of the JOBS_PER_RUN, and the worker never burns a real tsenta attempt on
# something already known to fail.
ZAPPLY_DEAD_END_URL = "https://zapply.jobs/jobs/"

# Upper bound on how many candidates one run will even look at (beyond
# JOBS_PER_RUN) while skipping dead zapplyjobs redirects - without this, a
# run during an unusually bad batch could resolve hundreds of redirects
# looking for JOBS_PER_RUN good ones.
MAX_CANDIDATES_CONSIDERED = JOBS_PER_RUN * 5

# Sources like zapplyjobs/speedyapply scrape every row in their table with no
# keyword filter of their own (fine for browsing - the swipe tabs are the
# filter there), so with no human reviewing before applying, auto-apply needs
# its own relevance check or it'll happily queue things like "Transportation
# Specialist" or "Machine Shop Supervisor" right alongside SWE roles.
#
# This has to be a SQL filter, not a Python filter over the newest N rows:
# a single source's sync can drop hundreds of same-timestamp "new" rows at
# once (zapplyjobs: 547), which would crowd every other source's actually-
# relevant jobs out of a recency-ordered top-N slice entirely.
RELEVANT_TITLE_KEYWORDS = cfg.RELEVANT_TITLE_KEYWORDS

# jobright/simplify list every office a posting is open to as one combined
# location string (e.g. "London, UK, NYC" or "Montreal, QC, Canada,
# Toronto, ON, Canada, ..., Remote in Canada") - there's no per-office
# split we could apply through, so a listing that mentions the UK or
# Canada at all is excluded rather than guessing it'd route to the US
# office. A NULL location (unknown) is let through rather than blocked -
# absence of location data isn't evidence it's non-US.
#
# Patterns are anchored to "UK"/"Canada" as their own comma-separated
# segment (", UK", "UK,", or "Remote in UK"), not a bare substring - a
# plain "%uk%" would also match it appearing inside an unrelated word
# (e.g. "uk" is a substring of "Milwaukee"). SQLite's LIKE is already
# case-insensitive for ASCII by default, so this still catches "uk"/"Uk".
def _country_segment_patterns(token):
    return (f"%, {token}", f"%, {token},%", token, f"{token},%", f"%in {token}%")


NON_US_LOCATION_PATTERNS = (
    *_country_segment_patterns("UK"),
    "%United Kingdom%",
    *_country_segment_patterns("Canada"),
)

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


def _not_blocked_filter():
    return and_(*(Job.job_url.notlike(p) for p in BLOCKED_URL_PATTERNS))


def _us_only_filter():
    non_us = or_(*(Job.location.like(p) for p in NON_US_LOCATION_PATTERNS))
    return or_(Job.location.is_(None), ~non_us)


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
    # The "Auto-apply running" start notification (added when this fired
    # 4x/day) turned into 12 pushes/day once the schedule went hourly
    # (2026-10-19) - disabled 2026-10-26 as too noisy. Real outcomes are
    # still visible on /history/auto; nothing about the run itself changed.

    for source in AUTO_APPLY_SOURCES:
        try:
            run_sync_sync(app, source)
        except Exception:
            # Already logged by run_sync_sync - one source failing to sync
            # shouldn't stop the others, or the apply step below.
            pass

    cutoff = datetime.utcnow() - timedelta(hours=RECENT_WINDOW_HOURS)
    candidates = (
        Job.query.filter_by(status="new")
        .filter(Job.source.in_(AUTO_APPLY_SOURCES))
        .filter(Job.created_at >= cutoff)
        .filter(_relevant_title_filter())
        .filter(_not_blocked_filter())
        .filter(_us_only_filter())
        .order_by(Job.created_at.desc())
        .limit(MAX_CANDIDATES_CONSIDERED)
        .all()
    )

    now = datetime.utcnow()
    queued, skipped_dead_zapply = 0, 0
    for job in candidates:
        if queued >= JOBS_PER_RUN:
            break

        if job.source == "zapplyjobs" and resolve_apply_link(job.job_url) == ZAPPLY_DEAD_END_URL:
            job.status = "apply_failed"
            job.status_changed_at = now
            job.applied_via = "auto"
            job.status_message = (
                "Skipped: zapplyjobs' redirect for this posting is already dead "
                f"(resolves to {ZAPPLY_DEAD_END_URL}) - not actually submitted"
            )
            skipped_dead_zapply += 1
            continue

        job.status = "queued"
        job.status_changed_at = now
        job.applied_via = "auto"
        queued += 1

    if queued or skipped_dead_zapply:
        db.session.commit()

    app.logger.info(
        "[auto-apply schedule] synced %d source(s), queued %d/%d job(s), "
        "skipped %d dead zapplyjobs redirect(s)",
        len(AUTO_APPLY_SOURCES), queued, JOBS_PER_RUN, skipped_dead_zapply,
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
