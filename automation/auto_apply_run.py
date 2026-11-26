"""One pass of the auto-apply scheduler.

    gather candidates -> run the filter rules -> drop duplicates -> cap -> apply -> report

Sources, best first: Tsenta's own feed (ordered by its match score), then Simplify
and JobRight rows the scrapers stored (newest first). Every candidate gets a
decision row (Pass / Skip, the rule and the reason) so a run can be audited.

DRY_RUN (config.DRY_RUN, on by default) does everything except the final apply
call: the Tsenta client is built with allow_apply=False, so even a bug here can't
spend a credit. Nothing is written to Job rows in a dry run either.

Two runs can't overlap: a threading lock for this process plus an flock on a file
for any other process (the manual `tools/auto_apply_run.py` pass).
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config as cfg
from automation import candidates as cand_mod
from automation import filters, push, schedule_state, tsenta_mcp
from automation.tsenta_oauth import NeedsLogin, find_needs_login
from db import db
from models import AutoApplyDecision, AutoApplyRun, Job

RUN_CAP = 10                      # applications per run (was 40; lowered 2026-11-25 so one run can't spend the day's budget)
DAILY_CAP = 60                    # applications per New York day
SCRAPER_SOURCES = ("simplify", "jobright")
LOCK_FILE = os.path.join(cfg.INSTANCE_DIR, "auto_apply.lock")
DAY_TZ = ZoneInfo("America/New_York")
DECISION_KEEP_DAYS = 14
DESCRIPTION_CHUNK = 20
OUTCOME_WAIT_S = 180
# What counts as "already applied to this company today": accepted by Tsenta (sent, in progress, or
# waiting on you) or Tsenta saying it already has one. Failed or refused attempts don't.
COMPANY_DAY_ACTIONS = ("applied", "pending", "needs_you", "already_applied")
# What counts toward the daily cap: Tsenta accepted the application (a credit was used).
SPENT_ACTIONS = ("applied", "pending", "needs_you", "failed")

_THREAD_LOCK = threading.Lock()


class RunLock:
    """Non-blocking, re-checkable lock that works across threads and processes."""

    def __init__(self, path=None):
        self.path = path or LOCK_FILE
        self._fd = None

    def acquire(self):
        if not _THREAD_LOCK.acquire(blocking=False):
            return False
        try:
            self._fd = open(self.path, "a+")
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._release_fd()
            _THREAD_LOCK.release()
            return False

    def _release_fd(self):
        if self._fd is not None:
            try:
                self._fd.close()
            finally:
                self._fd = None

    def release(self):
        self._release_fd()
        _THREAD_LOCK.release()


def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt and dt.tzinfo else dt


def _today_start_utc(now_utc):
    local = now_utc.replace(tzinfo=timezone.utc).astimezone(DAY_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)


CAP_RESET_KEY = "auto_apply_cap_reset_since"


def _cap_window_start(now_utc):
    """Start of the current daily-cap window: midnight New York, or later if the cap was reset today
    (tools/reset_daily_cap.py). A reset only moves the start within the same day, so it expires by itself."""
    start = _today_start_utc(now_utc)
    try:
        saved = schedule_state.load(CAP_RESET_KEY)
        reset = datetime.fromisoformat(saved) if saved else None
    except (TypeError, ValueError):
        reset = None
    return max(start, reset) if reset else start


def applications_today(now_utc=None):
    """Applications Tsenta accepted in the current daily-cap window (the New York day, unless reset) in real runs."""
    since = _cap_window_start(now_utc or datetime.utcnow())
    return (
        AutoApplyDecision.query.join(AutoApplyRun, AutoApplyRun.id == AutoApplyDecision.run_id)
        .filter(AutoApplyRun.dry_run.is_(False), AutoApplyDecision.created_at >= since, AutoApplyDecision.action.in_(SPENT_ACTIONS))
        .count()
    )


def companies_applied_today(now_utc=None):
    """Company keys the scheduler has applied to today (New York day) in real, non-dry runs."""
    since = _today_start_utc(now_utc or datetime.utcnow())
    rows = (
        db.session.query(AutoApplyDecision.company)
        .join(AutoApplyRun, AutoApplyRun.id == AutoApplyDecision.run_id)
        .filter(AutoApplyRun.dry_run.is_(False), AutoApplyDecision.created_at >= since, AutoApplyDecision.action.in_(COMPANY_DAY_ACTIONS))
        .all()
    )
    return {k for k in (filters.company_key(r[0]) for r in rows) if k}


# ------------------------------------------------------------------ candidates

def _seed_duplicates():
    """Everything already applied to, queued or in flight - by URL and by company/title/place."""
    jobs = Job.query.filter(Job.status.in_(("applied", "queued", "applying", "apply_failed", "needs_review"))).all()
    urls = [j.job_url for j in jobs]
    roles = [(j.company, j.title, [j.location] if j.location else []) for j in jobs]
    return filters.DuplicateFilter(urls, roles)


def _scraper_candidates():
    jobs = (
        Job.query.filter(Job.status == "new", Job.source.in_(SCRAPER_SOURCES))
        .order_by(Job.created_at.desc()).all()
    )
    return [cand_mod.from_job(j) for j in jobs]


def _evaluate_tsenta(client, rows, now, want_all):
    """Rules on Tsenta feed rows. Cheap rules first without a description; only the rows still
    in the running get their description fetched (best score first) and are judged again.
    Returns [(Candidate, Verdict)] in score order."""
    cands = [cand_mod.from_tsenta(r) for r in rows]
    cands.sort(key=lambda c: -(c.match_score or 0))
    verdicts = {id(c): filters.evaluate(c, now) for c in cands}
    contenders = [c for c in cands if verdicts[id(c)].passed]
    for start in range(0, len(contenders), DESCRIPTION_CHUNK):
        chunk = contenders[start:start + DESCRIPTION_CHUNK]
        texts = client.descriptions([c.ref for c in chunk])
        for c in chunk:
            c.description = texts.get(c.ref) or c.description
            verdicts[id(c)] = filters.evaluate(c, now)
    return [(c, verdicts[id(c)]) for c in cands]


# ------------------------------------------------------------------ recording

def _age_minutes(cand, now):
    age = filters.estimated_age(cand, now)
    return None if age is None else int(age.total_seconds() // 60)


def _freshness_key(item):
    """Newest first; jobs whose age is unknown go last; equal ages go to the better match."""
    cand, _ = item
    age = _age_minutes(cand, None)
    return (age is None, age if age is not None else 0, -(cand.match_score or 0))


def _record(run, cand, verdict, action="none", message=None, job_id=None, now=None):
    row = AutoApplyDecision(
        posted_at=_naive_utc(cand.posted_at), age_minutes=_age_minutes(cand, now),
        run_id=run.id, source=cand.source, title=(cand.title or "")[:500], company=cand.company,
        location="; ".join(cand.locations or [])[:255], url=cand.url, match_score=cand.match_score, job_id=job_id,
        tsenta_job_id=cand.ref if cand.source == "tsenta" else None,
        verdict="pass" if verdict.passed else "skip", category=verdict.category, reason=verdict.reason,
        action=action, message=message,
    )
    db.session.add(row)
    return row


def _job_fields(cand):
    return dict(
        posted_at=_naive_utc(cand.posted_at), years_min=cand.years_min, sponsorship=cand.sponsorship,
        match_score=cand.match_score,
    )


def _status_for(outcome):
    return {"applied": "applied", "failed": "apply_failed", "needs_you": "needs_review", "pending": "applying"}[outcome.kind]


def _outcome_message(outcome):
    text = outcome.message or outcome.label or outcome.status
    if outcome.kind == "failed" and outcome.will_retry:
        text += " (Tsenta says it will retry this by itself)"
    return f"Tsenta: {text}" if text else "Tsenta"


def _apply_outcome_to(job, decision, outcome):
    job.status = _status_for(outcome)
    job.status_message = _outcome_message(outcome)
    job.status_changed_at = datetime.utcnow()
    decision.action = {"applied": "applied", "failed": "failed", "needs_you": "needs_you", "pending": "pending"}[outcome.kind]
    decision.message = job.status_message


# ------------------------------------------------------------------ live apply

def _job_for(cand, now):
    """The Job row to carry this application: the scraper's own, or a new row for a feed job."""
    if cand.source == "tsenta":
        job = Job.query.filter_by(source="tsenta", external_id=str(cand.ref)).first()
        if job is None:
            job = Job(source="tsenta", external_id=str(cand.ref), title=cand.title, company=cand.company,
                      location="; ".join(cand.locations or [])[:255], job_url=cand.url, created_at=now)
            db.session.add(job)
    else:
        job = db.session.get(Job, cand.ref)
    for key, value in _job_fields(cand).items():
        setattr(job, key, value)
    if cand.source == "tsenta":
        job.tsenta_job_id = str(cand.ref)
    return job


def _mark_submitted(job, decision, result, now):
    job.applied_via = "auto"
    job.status_changed_at = now
    if result.success:
        job.status = "applying"
        job.status_message = "Sent to Tsenta; waiting for its result"
        job.tsenta_application_id = result.application_id
        decision.action = "pending"
        decision.message = job.status_message
        return
    kind = result.category
    job.status = "apply_failed"
    job.status_message = f"Tsenta: {result.message or kind} [{kind}]"
    decision.action = "already_applied" if kind == "already_applied" else "rejected"
    decision.message = job.status_message
    if kind == "already_applied":
        job.status = "applied"


def _scraper_apply(client, cand):
    try:
        status, description = client.fetch_job_description(cand.url)
    except tsenta_mcp.TsentaError:
        status, description = "failed", None
    if status == "expired":
        return tsenta_mcp.ApplyResult(cand.url, False, "closed", "Tsenta says this posting is closed")
    return client.apply_url(cand.url, description)


def _apply_live(run, client, chosen, now):
    """chosen: [(Candidate, decision)]. Returns True when the run should stop (no credits / incomplete profile)."""
    stop = None
    feed = [(c, d) for c, d in chosen if c.source == "tsenta"]
    other = [(c, d) for c, d in chosen if c.source != "tsenta"]
    submitted = []                                       # [(job, decision)]

    if feed:
        jobs = {str(c.ref): (c, d) for c, d in feed}
        for result in client.apply_jobs([c.ref for c, _ in feed]):
            cand, decision = jobs[str(result.ref)]
            if result.stop_run:
                stop = stop or result
            job = _job_for(cand, now) if (result.success or result.category in ("already_applied",)) else None
            if job is not None:
                db.session.flush()
                decision.job_id = job.id
                _mark_submitted(job, decision, result, now)
            else:
                decision.action = "rejected"
                decision.message = f"Tsenta: {result.message or result.category} [{result.category}]"
            if result.success:
                submitted.append((job, decision))
        db.session.commit()                              # recorded before we wait, so a crash can't re-apply

    for cand, decision in other:
        if stop:
            decision.action = "capped"
            decision.message = f"not attempted: {stop.message}"
            continue
        result = _scraper_apply(client, cand)
        if result.stop_run:
            stop = result
        job = _job_for(cand, now)
        db.session.flush()
        decision.job_id = job.id
        _mark_submitted(job, decision, result, now)
        db.session.commit()
        if result.success:
            submitted.append((job, decision))

    app_ids = [j.tsenta_application_id for j, _ in submitted if j.tsenta_application_id]
    if app_ids:
        outcomes = client.wait_for_outcomes(app_ids, timeout_s=OUTCOME_WAIT_S)
        for job, decision in submitted:
            outcome = outcomes.get(job.tsenta_application_id)
            if outcome is not None and outcome.kind != "pending":
                _apply_outcome_to(job, decision, outcome)
            elif outcome is not None:
                job.status_message = _outcome_message(outcome)
                decision.message = job.status_message
        db.session.commit()
    return stop


VERIFY_WINDOW_DAYS = 3


def reconcile(client, now=None):
    """Check what Tsenta itself says about applications we already recorded (read-only on Tsenta).

    - Rows with a Tsenta application id that are still in flight, waiting on you, or failed are re-read
      (a "will retry" failure can later succeed).
    - Jobs marked applied/applying in the last few days with no application id (a manual apply through
      the MCP) are matched to Tsenta's list by URL. If Tsenta says it failed, the job becomes "Failed"
      with Tsenta's own reason; if it says completed, the id is stored.
    Jobs Tsenta has no record of are left alone (older browser-flow applies never appear there)."""
    now = now or datetime.utcnow()
    tracked = Job.query.filter(
        Job.tsenta_application_id.isnot(None), Job.status.in_(("applying", "needs_review", "apply_failed")),
    ).all()
    untracked = Job.query.filter(
        Job.tsenta_application_id.is_(None), Job.status.in_(("applied", "applying")),
        Job.applied_via.in_(("auto", "manual")), Job.status_changed_at >= now - timedelta(days=VERIFY_WINDOW_DAYS),
    ).all()
    if not tracked and not untracked:
        return 0
    rows = client.application_rows(pages=5)
    by_id = {r.get("applicationId"): r for r in rows}
    by_url = {}
    for r in rows:
        key = filters.normalize_url(r.get("url") or "")
        if key:
            by_url.setdefault(key, r)                      # newest first, so the latest attempt wins
    changed = 0
    for job in tracked + untracked:
        row = by_id.get(job.tsenta_application_id) if job.tsenta_application_id else by_url.get(filters.normalize_url(job.job_url))
        if row is None:
            continue
        outcome = tsenta_mcp.outcome_from_row(row)
        if outcome.kind == "pending" and job.tsenta_application_id:
            continue
        job.tsenta_application_id = job.tsenta_application_id or outcome.application_id
        status = _status_for(outcome)
        message = _outcome_message(outcome)
        if outcome.kind == "applied" and job.status == "applied":
            status, message = job.status, job.status_message          # already right; keep what we wrote
        if (job.status, job.status_message) != (status, message):
            job.status, job.status_message, job.status_changed_at = status, message, datetime.utcnow()
            changed += 1
    db.session.commit()
    return changed


# ------------------------------------------------------------------ the run

def _default_client(allow_apply):
    return tsenta_mcp.TsentaClient.from_env(allow_apply=allow_apply)


def _age_text(minutes):
    return f"{minutes}m" if minutes < 120 else f"{minutes / 60:.1f}h"


def _summary(run):
    skipped = run.skipped
    skip_text = ", ".join(f"{k} {v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none"
    head = f"[DRY RUN] " if run.dry_run else ""
    done = f"would apply {run.would_apply}" if run.dry_run else f"applied {run.applied}"
    body = f"found {run.found} · passed {run.passed} · {done} · failed {run.failed}"
    if run.needs_you:
        body += f" · needs you {run.needs_you}"
    capped = run.capped
    if capped:
        body += f" · passed but not applied (cap) {capped}"
    if run.median_age_min is not None:
        body += f" · median age {_age_text(run.median_age_min)}"
    body += f"\nskipped: {skip_text}"
    if run.message:
        body += f"\n{run.message}"
    return f"{head}Auto-apply run", body


def _notify(app, run):
    try:
        title, body = _summary(run)
        push.send_to_all(title, body, url=f"/history/auto/runs/{run.id}")
    except Exception:
        app.logger.exception("[auto-apply] push notification failed")


CAP_STATE_KEY = "auto_apply_cap_notified_date"


def _notify_daily_cap(app, run, now=None):
    """One push per New York day, the first time the day's cap of DAILY_CAP applications is reached
    in a live run. Later runs that day (which find no room) stay quiet."""
    if run.dry_run:
        return
    try:
        today = (now or datetime.now(DAY_TZ)).astimezone(DAY_TZ).date().isoformat()
        sent = applications_today()
        if sent < DAILY_CAP or schedule_state.load(CAP_STATE_KEY) == today:
            return
        schedule_state.save(CAP_STATE_KEY, today)                     # marked first, so a push failure can't repeat
        push.send_to_all(
            f"Daily cap reached: {sent}/{DAILY_CAP} applications",
            (f"{run.capped} job{'s' if run.capped != 1 else ''} passed every rule in this run but could not be applied to. " if run.capped else "")
            + "No more applications today; the scheduler resumes tomorrow. Jobs that pass in the meantime are left for the next day's runs.",
            url=f"/history/auto/runs/{run.id}",
        )
    except Exception:
        app.logger.exception("[auto-apply] daily-cap notification failed")


def _prune():
    cutoff = datetime.utcnow() - timedelta(days=DECISION_KEEP_DAYS)
    AutoApplyDecision.query.filter(AutoApplyDecision.created_at < cutoff).delete()
    db.session.commit()


def run_once(app, *, trigger="schedule", dry_run=None, client_factory=None, sync=True, now=None, lock=None, notify=True):
    """Do one pass. Returns the AutoApplyRun, or None if another run holds the lock."""
    lock = lock or RunLock()
    if not lock.acquire():
        app.logger.info("[auto-apply] another run is in progress - skipping this one")
        return None
    try:
        return _run_locked(app, trigger, cfg.DRY_RUN if dry_run is None else dry_run, client_factory or _default_client, sync, now, notify)
    finally:
        lock.release()


def _run_locked(app, trigger, dry, client_factory, sync, now, notify):
    now = now or datetime.now(timezone.utc)
    run = AutoApplyRun(trigger=trigger, dry_run=dry, status="running")
    db.session.add(run)
    db.session.commit()
    notes = []
    skipped = Counter()
    try:
        if sync:
            from automation.tasks import run_sync_sync
            for source in SCRAPER_SOURCES:
                try:
                    run_sync_sync(app, source)
                except Exception as exc:                  # already logged; the other source and Tsenta still count
                    notes.append(f"{source} sync failed: {exc}")
        _execute(app, run, dry, client_factory, now, notes, skipped)
    except NeedsLogin as exc:
        db.session.rollback()
        run.status = "needs_login"
        notes.append(f"Tsenta sign-in expired - run: {exc.args[0] if exc.args else '.venv/bin/python tools/tsenta_login.py'}")
    except Exception as exc:
        db.session.rollback()
        needs = find_needs_login(exc)
        if needs:
            run.status = "needs_login"
            notes.append(f"Tsenta sign-in expired - {needs}")
        else:
            run.status = "error"
            notes.append(f"error: {exc}")
            app.logger.exception("[auto-apply] run failed")
    run = db.session.merge(run)
    if run.status == "running":
        run.status = "done"
    run.skipped_json = json.dumps(dict(skipped))
    run.message = " | ".join(notes) or None
    run.finished_at = datetime.utcnow()
    db.session.commit()
    try:
        _prune()
    except Exception:
        db.session.rollback()
    app.logger.info("[auto-apply] run %s (%s) %s: found=%s passed=%s applied=%s would=%s failed=%s",
                    run.id, "dry" if dry else "live", run.status, run.found, run.passed, run.applied, run.would_apply, run.failed)
    if notify:
        _notify(app, run)
        _notify_daily_cap(app, run)
    return run


def _execute(app, run, dry, client_factory, now, notes, skipped):
    client = client_factory(not dry)
    scrapers = _scraper_candidates()
    with client:
        balance = client.balance()
        run.balance_before = balance.total
        reconcile(client)

        feed_rows = []
        try:
            feed_rows = client.feed()
            notes.extend(getattr(client, "last_feed_notes", []) or [])
        except tsenta_mcp.TsentaError as exc:
            if find_needs_login(exc):
                raise
            notes.append(f"Tsenta feed unavailable: {exc}")
        judged = _evaluate_tsenta(client, feed_rows, now, want_all=dry)
        judged += [(c, filters.evaluate(c, now)) for c in scrapers]

        # Newest first across every source (best match breaks ties), so the per-run cap (RUN_CAP) is spent on
        # fresh postings and the same-job duplicate check below keeps the earliest-seen copy.
        judged.sort(key=_freshness_key)
        dup = _seed_duplicates()
        passers = []
        for cand, verdict in judged:
            if verdict.passed:
                reason = dup.check(cand, now)
                if reason:
                    verdict = filters.Verdict(False, "duplicate", reason)
                else:
                    dup.remember(cand)
                    passers.append((cand, verdict))
                    continue
            skipped[verdict.category] += 1
            _record(run, cand, verdict, now=now)

        # One application per company per New York day: the freshest job (the list is newest first) wins.
        taken = companies_applied_today()
        eligible = []
        for cand, verdict in passers:
            key = filters.company_key(cand.company)
            if key and key in taken:
                verdict = filters.Verdict(False, "company-limit", f"already applied (or about to) to {cand.company} today - one application per company per day")
                skipped["company-limit"] += 1
                _record(run, cand, verdict, now=now)
                continue
            if key:
                taken.add(key)
            eligible.append((cand, verdict))
        passers = eligible

        room = DAILY_CAP - applications_today()
        budget = max(0, min(RUN_CAP, room))
        if not balance.has_room:
            budget = 0
            notes.append("Tsenta has no applications left" + (f" (resets {balance.cycle_end})" if balance.cycle_end else ""))
        elif not balance.admin_bypass:
            budget = min(budget, balance.total)
        if room <= 0:
            notes.append(f"daily cap of {DAILY_CAP} reached")

        chosen, rest = passers[:budget], passers[budget:]
        decisions = []
        for cand, verdict in chosen:
            decisions.append((cand, _record(run, cand, verdict, action="would_apply" if dry else "none", now=now)))
        for cand, verdict in rest:
            _record(run, cand, verdict, action="capped", message="over the run/daily/balance cap", now=now)
        ages = sorted(a for a in (_age_minutes(c, now) for c, _ in chosen) if a is not None)
        run.median_age_min = ages[len(ages) // 2] if ages else None
        run.found = len(judged)
        run.passed = len(passers)
        db.session.commit()

        if dry:
            run.would_apply = len(chosen)
        else:
            stop = _apply_live(run, client, decisions, datetime.utcnow())
            if stop:
                notes.append(f"stopped early: {stop.message} [{stop.category}]")
                if stop.category == "out_of_balance":
                    run.status = "no_balance"
        db.session.commit()
        counts = Counter(d.action for _, d in decisions)
        if not dry:
            run.applied = counts["applied"] + counts["pending"]
            run.failed = counts["failed"] + counts["rejected"]
            run.needs_you = counts["needs_you"]
        try:
            run.balance_after = client.balance().total
        except tsenta_mcp.TsentaError:
            pass
