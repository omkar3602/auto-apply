"""The scheduler's pass end to end: a temporary database, a fake Tsenta client, no network."""
import json
import threading
from datetime import datetime, timedelta, timezone

import pytest

import config as cfg
from app_factory import create_app
from automation import auto_apply_run as run_mod
from automation import auto_apply_schedule as sched
from automation import tsenta_mcp as tm
from automation.tsenta_oauth import NeedsLogin
from db import db
from automation.auto_apply_run import RUN_CAP
from models import AutoApplyDecision, AutoApplyRun, Job


def iso(hours_ago=1):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


POSTED = iso()                                      # one fixed "an hour ago" so same-age rows tie exactly


def feed_row(n, score=80, title="Software Engineer", **kw):
    row = {"id": f"t{n}", "title": title, "companyName": f"Acme{n}", "companyWebsite": f"acme{n}.com", "url": f"https://acme{n}.example/jobs/{n}",
           "locations": ["New York, NY"], "matchScore": score, "datePosted": POSTED, "isSupportedAts": True, "yearsOfExperienceMin": 2,
           "sponsorship": "UNKNOWN", "clearanceRequired": False}
    row.update(kw)
    return row


class FakeClient:
    """Stands in for TsentaClient; records everything it is asked to do."""

    def __init__(self, allow_apply, rows=(), credits=500, outcome_kind="applied", descriptions=None, apply_category=None):
        self.allow_apply = allow_apply
        self.rows, self.credits, self.outcome_kind = list(rows), credits, outcome_kind
        self.descriptions_map = descriptions or {}
        self.apply_category = apply_category
        self.last_feed_notes = []
        self.applied_ids, self.applied_urls, self.description_calls, self.outcome_calls = [], [], [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def balance(self):
        return tm.Balance(total=self.credits, monthly_remaining=self.credits, monthly_limit=1500, cycle_end="2026-12-18", admin_bypass=False)

    def feed(self, **kw):
        return list(self.rows)

    def descriptions(self, ids, max_chars=6000):
        self.description_calls.append(list(ids))
        return {i: self.descriptions_map.get(i, "A fine job working with Python.") for i in ids}

    def fetch_job_description(self, url):
        return "success", f"posting at {url}"

    def _result(self, ref, n):
        if self.apply_category:
            return tm.ApplyResult(ref, False, self.apply_category, f"refused: {self.apply_category}")
        self.credits -= 1
        return tm.ApplyResult(ref, True, "applied", application_id=f"app-{n}")

    def apply_jobs(self, ids):
        if not self.allow_apply:
            raise tm.ApplyBlocked("dry run")
        self.applied_ids += list(ids)
        return [self._result(i, len(self.applied_ids) + k) for k, i in enumerate(ids)]

    def apply_url(self, url, description=None):
        if not self.allow_apply:
            raise tm.ApplyBlocked("dry run")
        self.applied_urls.append(url)
        return self._result(url, 100 + len(self.applied_urls))

    rows = ()

    def application_rows(self, pages=5):
        return list(self.rows_list) if hasattr(self, "rows_list") else []

    def wait_for_outcomes(self, ids, timeout_s=0, **kw):
        self.outcome_calls.append(list(ids))
        return {i: tm.Outcome(application_id=i, status="COMPLETED" if self.outcome_kind == "applied" else "FAILED", kind=self.outcome_kind,
                              message="done" if self.outcome_kind == "applied" else "Could not submit. It will be retried automatically.",
                              will_retry=self.outcome_kind == "failed") for i in ids}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(run_mod, "LOCK_FILE", str(tmp_path / "lock"))
    monkeypatch.setattr(cfg, "DRY_RUN", True)
    app = create_app({"SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}", "TESTING": True}, background=False)
    pushes = []
    monkeypatch.setattr(run_mod.push, "send_to_all", lambda title, body, url="/": pushes.append((title, body, url)) or (1, 0, 0))
    app.pushes = pushes
    state = {}
    monkeypatch.setattr(run_mod.schedule_state, "load", lambda k: state.get(k))
    monkeypatch.setattr(run_mod.schedule_state, "save", lambda k, v: state.__setitem__(k, v))
    app.state = state
    with app.app_context():
        yield app
        db.session.remove()


def go(app, client, **kw):
    kw.setdefault("sync", False)
    return run_mod.run_once(app, client_factory=lambda allow: (setattr(client, "allow_apply", allow), client)[1], lock=run_mod.RunLock(), **kw)


def scraper_job(n, source="simplify", title="Software Engineer", **kw):
    raw = {"date_posted": int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()), "sponsorship": "Other"} if source == "simplify" else {}
    job = Job(source=source, external_id=f"{source}-{n}", title=title, company=f"Beta{n}", location="New York, NY", job_url=f"https://beta{n}.example/apply/{n}",
              posted_label="2 hours ago", status="new", raw_json=json.dumps(raw))
    job.__dict__.update(kw)
    db.session.add(job)
    db.session.commit()
    return job


# ------------------------------------------------------------------ dry run

def test_dry_run_decides_everything_and_applies_nothing(app):
    client = FakeClient(True, rows=[feed_row(1, 90), feed_row(2, 70, title="Marketing Manager"), feed_row(3, 85, locations=["London, UK"])])
    job = scraper_job(1)
    run = go(app, client)
    assert run.dry_run and run.status == "done"
    assert client.applied_ids == [] and client.applied_urls == [] and client.allow_apply is False
    assert (run.found, run.passed, run.would_apply, run.applied) == (4, 2, 2, 0)
    assert json.loads(run.skipped_json) == {"title": 1, "location": 1}
    rows = {d.title + d.company: d for d in AutoApplyDecision.query.all()}
    assert rows["Software EngineerAcme1"].action == "would_apply" and rows["Software EngineerAcme1"].verdict == "pass"
    assert rows["Marketing ManagerAcme2"].verdict == "skip" and rows["Marketing ManagerAcme2"].category == "title" and rows["Marketing ManagerAcme2"].reason
    assert Job.query.filter_by(source="tsenta").count() == 0                       # no rows invented for feed jobs
    assert db.session.get(Job, job.id).status == "new" and db.session.get(Job, job.id).applied_via is None


def test_dry_run_is_the_default_when_not_told_otherwise(app):
    client = FakeClient(True, rows=[feed_row(1)])
    run = run_mod.run_once(app, client_factory=lambda allow: (setattr(client, "allow_apply", allow), client)[1], lock=run_mod.RunLock(), sync=False)
    assert run.dry_run and client.allow_apply is False


def test_feed_ties_on_age_go_to_the_best_score_and_descriptions_only_for_contenders(app):
    rows = [feed_row(1, 50), feed_row(2, 95), feed_row(3, 70, title="Chef"), feed_row(4, 80)]
    client = FakeClient(True, rows=rows)
    go(app, client)
    assert client.description_calls == [["t2", "t4", "t1"]]                        # the Chef never gets fetched
    order = [d.tsenta_job_id for d in AutoApplyDecision.query.filter_by(verdict="pass").order_by(AutoApplyDecision.id)]
    assert order == ["t2", "t4", "t1"]


def test_description_rules_apply_after_the_fetch(app):
    client = FakeClient(True, rows=[feed_row(1)], descriptions={"t1": "We are unable to provide visa sponsorship for this role."})
    run = go(app, client)
    assert run.passed == 0 and json.loads(run.skipped_json) == {"sponsorship": 1}


# ------------------------------------------------------------------ live

def test_live_applies_records_auto_and_waits_for_the_real_outcome(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1, 90), feed_row(2, 80)])
    sj = scraper_job(1)
    run = go(app, client)
    assert not run.dry_run and (run.applied, run.failed, run.needs_you) == (3, 0, 0)
    assert client.applied_ids == ["t1", "t2"] and client.applied_urls == [sj.job_url]
    jobs = {j.external_id: j for j in Job.query.all()}
    assert jobs["t1"].source == "tsenta" and jobs["t1"].status == "applied" and jobs["t1"].applied_via == "auto"
    assert jobs["t1"].tsenta_job_id == "t1" and jobs["t1"].match_score == 90 and jobs["t1"].tsenta_application_id
    assert jobs[sj.external_id].status == "applied" and jobs[sj.external_id].applied_via == "auto"
    assert len(client.outcome_calls) == 1 and len(client.outcome_calls[0]) == 3
    assert run.balance_before == 500 and run.balance_after == 497


def test_a_failure_tsenta_reports_is_recorded_with_its_own_words(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1)], outcome_kind="failed")
    run = go(app, client)
    assert (run.applied, run.failed) == (0, 1)
    job = Job.query.filter_by(external_id="t1").one()
    assert job.status == "apply_failed" and "retried automatically" in job.status_message and "will retry" in job.status_message


def test_an_application_waiting_on_you_is_flagged_not_failed(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1)], outcome_kind="needs_you")
    run = go(app, client)
    assert (run.needs_you, run.failed, run.applied) == (1, 0, 0)
    assert Job.query.filter_by(external_id="t1").one().status == "needs_review"


@pytest.mark.parametrize("category", ["closed", "unsupported", "error"])
def test_a_refused_apply_is_a_failure_and_costs_nothing_toward_the_cap(app, monkeypatch, category):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1)], apply_category=category)
    run = go(app, client)
    assert run.failed == 1 and run.applied == 0
    assert run_mod.applications_today() == 0


def test_running_out_of_credits_stops_the_scraper_applies_too(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1)], apply_category="out_of_balance")
    scraper_job(1)
    run = go(app, client)
    assert client.applied_urls == [] and run.applied == 0
    d = AutoApplyDecision.query.filter_by(source="simplify").one()
    assert d.action == "capped" and "not attempted" in d.message
    assert run.status == "no_balance"


# ------------------------------------------------------------------ caps and order

def fill_runs(app, count, start=1):
    """`count` live runs, each offered more than RUN_CAP fresh jobs from distinct companies."""
    clients = []
    for k in range(count):
        base = start + k * 100
        client = FakeClient(True, rows=[feed_row(base + i, score=100 - i) for i in range(RUN_CAP + 5)])
        go(app, client)
        clients.append(client)
    return clients


def test_a_run_applies_to_at_most_10_best_scores_first(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    assert RUN_CAP == 10
    client = FakeClient(True, rows=[feed_row(i, score=i) for i in range(1, 31)])
    run = go(app, client)
    assert len(client.applied_ids) == 10 and client.applied_ids[0] == "t30" and "t21" in client.applied_ids and "t20" not in client.applied_ids
    assert (run.passed, run.applied) == (30, 10)
    assert AutoApplyDecision.query.filter_by(action="capped").count() == 20
    assert run.capped == 20 and "passed but not applied (cap) 20" in app.pushes[-1][1]   # the push says how many were shorted out
    page = app.test_client().get(f"/history/auto/runs/{run.id}").get_data(as_text=True)
    assert "20 not applied because a cap was full" in page
    assert "Not applied (cap)" in app.test_client().get("/history/auto/runs").get_data(as_text=True)


def test_a_run_that_fits_under_the_cap_reports_nothing_left_out(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    run = go(app, FakeClient(True, rows=[feed_row(i) for i in range(1, 4)]))
    assert run.capped == 0 and "not applied" not in app.pushes[-1][1]


def test_the_daily_cap_of_60_holds_across_runs(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    clients = fill_runs(app, 6)
    assert [len(c.applied_ids) for c in clients] == [10] * 6
    assert run_mod.applications_today() == 60
    seventh = FakeClient(True, rows=[feed_row(i) for i in range(701, 710)])
    run7 = go(app, seventh)
    assert seventh.applied_ids == [] and "daily cap" in run7.message


def test_resetting_the_cap_for_today_lets_runs_apply_again_and_expires_with_the_day(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    fill_runs(app, 6)
    assert run_mod.applications_today() == 60
    blocked = FakeClient(True, rows=[feed_row(i) for i in range(2001, 2005)])
    go(app, blocked)
    assert blocked.applied_ids == []
    app.state[run_mod.CAP_RESET_KEY] = (datetime.utcnow() + timedelta(seconds=1)).isoformat()   # what the tool writes
    import time; time.sleep(1.2)
    assert run_mod.applications_today() == 0
    again = FakeClient(True, rows=[feed_row(i) for i in range(2101, 2125)])
    go(app, again)
    assert len(again.applied_ids) == 10 and run_mod.applications_today() == 10
    # a reset left over from yesterday changes nothing
    app.state[run_mod.CAP_RESET_KEY] = (datetime.utcnow() - timedelta(days=1)).isoformat()
    assert run_mod.applications_today() == 70
    # the company limit is separate: a company applied to earlier today is still off limits
    assert "company-limit" in json.dumps(json.loads(go(app, FakeClient(True, rows=[feed_row(2999, companyName="Acme2101", title="Backend Engineer")])).skipped_json))


def test_the_last_run_before_the_cap_only_gets_what_is_left(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    fill_runs(app, 5)
    app.pushes.clear()
    last = FakeClient(True, rows=[feed_row(i) for i in range(801, 820)])
    go(app, last)
    assert len(last.applied_ids) == 10 and run_mod.applications_today() == 60


def test_one_notification_the_day_the_cap_is_hit(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    fill_runs(app, 5)
    assert not any("Daily cap" in p[0] for p in app.pushes)                         # 50 of 60: only run summaries
    fill_runs(app, 1, start=900)                                                    # reaches 60
    assert sum("Daily cap reached: 60/60" in p[0] for p in app.pushes) == 1
    go(app, FakeClient(True, rows=[feed_row(i) for i in range(1001, 1010)]))        # a later run the same day
    assert sum("Daily cap reached" in p[0] for p in app.pushes) == 1


def test_the_cap_notification_comes_again_the_next_day(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    app.state[run_mod.CAP_STATE_KEY] = "2000-01-01"
    fill_runs(app, 6)
    assert sum("Daily cap reached" in p[0] for p in app.pushes) == 1


def test_a_dry_run_never_sends_the_cap_notification(app):
    go(app, FakeClient(True, rows=[feed_row(i) for i in range(1, 60)]))
    assert not any("Daily cap" in p[0] for p in app.pushes)


def test_dry_runs_do_not_use_up_the_daily_cap(app):
    go(app, FakeClient(True, rows=[feed_row(i) for i in range(1, 30)]))
    assert run_mod.applications_today() == 0


def test_the_budget_never_exceeds_the_credits_left(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(i) for i in range(1, 11)], credits=3)
    go(app, client)
    assert len(client.applied_ids) == 3


def test_no_credits_means_no_applies_and_a_note(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    client = FakeClient(True, rows=[feed_row(1)], credits=0)
    run = go(app, client)
    assert client.applied_ids == [] and "no applications left" in run.message


def test_newest_first_across_every_source_with_score_breaking_ties(app):
    rows = [feed_row(1, 99, datePosted=iso(22)), feed_row(2, 60, datePosted=iso(0.2)), feed_row(3, 70, datePosted=iso(2)), feed_row(4, 90, datePosted=iso(2))]
    scraper_job(1)                                                       # simplify, posted ~2h ago per its raw date
    run = go(app, FakeClient(True, rows=rows))
    order = [d.tsenta_job_id or d.source for d in AutoApplyDecision.query.filter_by(verdict="pass").order_by(AutoApplyDecision.id)]
    assert order[0] == "t2" and order[-1] == "t1"                        # 12 minutes old first, 22 hours old last
    assert order.index("t4") < order.index("t3")                         # same age: better match first
    assert set(order[1:4]) == {"t4", "t3", "simplify"}


def test_the_cap_is_spent_on_fresh_postings_not_old_high_scores(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    rows = [feed_row(i, 99, datePosted=iso(20)) for i in range(1, 21)] + [feed_row(100 + i, 50, datePosted=iso(0.5)) for i in range(1, 6)]
    client = FakeClient(True, rows=rows)
    go(app, client)
    assert len(client.applied_ids) == 10
    assert all(f"t{100 + i}" in client.applied_ids for i in range(1, 6))


def test_age_is_recorded_per_candidate_and_as_a_median_for_the_run(app):
    rows = [feed_row(1, datePosted=iso(0.5)), feed_row(2, datePosted=iso(1)), feed_row(3, datePosted=iso(3))]
    run = go(app, FakeClient(True, rows=rows))
    ages = {d.tsenta_job_id: d.age_minutes for d in AutoApplyDecision.query.all()}
    assert 28 <= ages["t1"] <= 32 and 58 <= ages["t2"] <= 62 and 178 <= ages["t3"] <= 182
    assert 58 <= run.median_age_min <= 62
    assert "median age" in app.pushes[0][1]
    page = app.test_client().get(f"/history/auto/runs/{run.id}").data.decode()
    assert "30m" in page or "29m" in page or "31m" in page


def test_postings_over_24_hours_old_are_skipped_whatever_their_score(app):
    run = go(app, FakeClient(True, rows=[feed_row(1, 99, datePosted=iso(30)), feed_row(2, 50, datePosted=iso(23))]))
    assert run.passed == 1 and json.loads(run.skipped_json) == {"recency": 1}
    assert "24h" in AutoApplyDecision.query.filter_by(tsenta_job_id="t1").one().reason


def test_jobs_with_no_known_age_go_last(app):
    scraper_job(1, posted_label=None, raw_json=json.dumps({}), source="jobright")
    run = go(app, FakeClient(True, rows=[feed_row(1)]))
    assert run.found == 2


# ------------------------------------------------------------------ rules and duplicates

def test_each_skip_is_recorded_under_its_rule(app):
    rows = [feed_row(1), feed_row(2, title="Staff Engineer"), feed_row(3, locations=["Toronto, Canada"]), feed_row(4, yearsOfExperienceMin=8),
            feed_row(5, url="https://www.linkedin.com/jobs/5"), feed_row(6, companyName="Amazon", companyWebsite="amazon.com"),
            feed_row(7, isSupportedAts=False), feed_row(8, sponsorship="NO_SPONSORSHIP")]
    run = go(app, FakeClient(True, rows=rows))
    skipped = json.loads(run.skipped_json)
    for category in ("title", "location", "experience", "blocked-domain", "no-apply-list", "unsupported-ats", "sponsorship"):
        assert skipped.get(category) == 1, (category, skipped)
    assert run.passed == 1


def test_never_applies_twice_to_the_same_job_or_cross_post(app):
    db.session.add(Job(source="tsenta", external_id="old", title="Software Engineer", company="Acme1", location="New York, NY", job_url="https://acme1.example/jobs/1",
                       status="applied", applied_via="auto"))
    db.session.commit()
    rows = [feed_row(1), feed_row(9, company_dummy=None, companyName="Acme9", url="https://other.example/x"), ]
    scraper = scraper_job(9)
    scraper.company = "Acme9"
    db.session.commit()
    run = go(app, FakeClient(True, rows=rows))
    skipped = json.loads(run.skipped_json)
    assert skipped.get("duplicate") == 2 and run.passed == 1                         # same URL as the applied one; scraper twin of t9


def test_one_application_per_company_per_day_freshest_wins(app):
    rows = [feed_row(1, companyName="Walmart Inc.", datePosted=iso(5), title="Data Engineer"), feed_row(2, companyName="WALMART", datePosted=iso(1)),
            feed_row(3, companyName="Walmart", datePosted=iso(9), title="Backend Engineer"), feed_row(4, companyName="Visa")]
    run = go(app, FakeClient(True, rows=rows))
    assert run.passed == 2 and json.loads(run.skipped_json) == {"company-limit": 2}
    winner = AutoApplyDecision.query.filter_by(verdict="pass").all()
    assert {d.tsenta_job_id for d in winner} == {"t2", "t4"}                       # the 1-hour-old Walmart job, and Visa
    skipped = AutoApplyDecision.query.filter_by(category="company-limit").all()
    assert {d.tsenta_job_id for d in skipped} == {"t1", "t3"} and "one application per company per day" in skipped[0].reason


def test_the_company_limit_carries_across_runs_and_counts_needs_you_and_already_applied(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    go(app, FakeClient(True, rows=[feed_row(1, companyName="Acme"), feed_row(2, companyName="Beta")], outcome_kind="needs_you"))
    second = FakeClient(True, rows=[feed_row(3, companyName="Acme Inc"), feed_row(4, companyName="Beta LLC"), feed_row(5, companyName="Gamma")])
    run = go(app, second)
    assert second.applied_ids == ["t5"] and json.loads(run.skipped_json) == {"company-limit": 2}
    # already-applied answers also count
    third = FakeClient(True, rows=[feed_row(6, companyName="Delta")], apply_category="already_applied")
    go(app, third)
    fourth = FakeClient(True, rows=[feed_row(7, companyName="Delta Corp")])
    assert go(app, fourth).passed == 0 and fourth.applied_ids == []


def test_failed_and_refused_applications_do_not_use_up_a_company(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    go(app, FakeClient(True, rows=[feed_row(1, companyName="Acme")], apply_category="error"))
    again = FakeClient(True, rows=[feed_row(2, companyName="Acme")])
    go(app, again)
    assert again.applied_ids == ["t2"]


def test_dry_runs_neither_count_toward_nor_are_blocked_by_the_company_limit(app, monkeypatch):
    go(app, FakeClient(True, rows=[feed_row(1, companyName="Acme")]))               # dry
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    live = FakeClient(True, rows=[feed_row(1, companyName="Acme")])
    go(app, live)
    assert live.applied_ids == ["t1"]


def test_a_missing_company_name_is_never_limited(app):
    rows = [feed_row(1, companyName=None), feed_row(2, companyName=None)]
    assert go(app, FakeClient(True, rows=rows)).passed == 2


def test_same_role_in_another_city_is_applied_to(app):
    rows = [feed_row(1, locations=["New York, NY"], companyName="Same", companyWebsite="same.com"),
            feed_row(2, locations=["Boston, MA"], companyName="Same", companyWebsite="same.com")]
    run = go(app, FakeClient(True, rows=rows))
    assert run.passed == 1 and json.loads(run.skipped_json) == {"company-limit": 1}   # different city is no longer enough: one per company per day


def test_zapply_and_speedyapply_are_no_longer_used(app):
    scraper_job(1, source="zapplyjobs")
    scraper_job(2, source="speedyapply")
    run = go(app, FakeClient(True, rows=[]))
    assert run.found == 0
    assert sched.AUTO_APPLY_SOURCES == ("simplify", "jobright")


# ------------------------------------------------------------------ errors and alerts

def test_an_expired_sign_in_ends_the_run_with_an_alert(app):
    class Dead(FakeClient):
        def balance(self):
            raise NeedsLogin("run: .venv/bin/python tools/tsenta_login.py")

    run = go(app, Dead(True))
    assert run.status == "needs_login" and "sign-in expired" in run.message
    assert "sign-in expired" in app.pushes[-1][1]


def test_one_scraper_failing_to_sync_does_not_stop_the_run(app, monkeypatch):
    import automation.tasks as tasks

    def fake_sync(app_, source):
        if source == "simplify":
            raise RuntimeError("github is down")

    monkeypatch.setattr(tasks, "run_sync_sync", fake_sync)
    run = go(app, FakeClient(True, rows=[feed_row(1)]), sync=True)
    assert run.status == "done" and run.passed == 1 and "simplify sync failed" in run.message


def test_a_feed_error_is_noted_and_scrapers_still_run(app):
    class Broken(FakeClient):
        def feed(self, **kw):
            raise tm.TsentaError("feed down")

    scraper_job(1)
    run = go(app, Broken(True))
    assert run.passed == 1 and "feed unavailable" in run.message


# ------------------------------------------------------------------ push summary

def test_one_push_per_run_with_the_counts_and_skip_reasons(app, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    go(app, FakeClient(True, rows=[feed_row(1), feed_row(2, title="Chef"), feed_row(3, locations=["London, UK"])]))
    assert len(app.pushes) == 1
    title, body, url = app.pushes[0]
    assert "DRY RUN" not in title
    assert "found 3" in body and "passed 1" in body and "applied 1" in body and "failed 0" in body
    assert "title 1" in body and "location 1" in body and url.startswith("/history/auto/runs/")


def test_a_dry_run_push_says_so(app):
    go(app, FakeClient(True, rows=[feed_row(1)]))
    title, body, _ = app.pushes[0]
    assert "[DRY RUN]" in title and "would apply 1" in body


def test_a_push_failure_does_not_break_the_run(app, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("push service down")
    monkeypatch.setattr(run_mod.push, "send_to_all", boom)
    assert go(app, FakeClient(True, rows=[feed_row(1)])).status == "done"


# ------------------------------------------------------------------ no overlap

def test_two_runs_cannot_overlap_in_process(app):
    inside, release = threading.Event(), threading.Event()

    class Slow(FakeClient):
        def balance(self):
            inside.set()
            release.wait(10)
            return super().balance()

    results = []
    t = threading.Thread(target=lambda: results.append(None) or _in_ctx(app, lambda: go(app, Slow(True))))
    t.start()
    assert inside.wait(10)
    assert go(app, FakeClient(True)) is None                                      # refused while the first is running
    release.set()
    t.join(10)
    assert AutoApplyRun.query.count() == 1


def _in_ctx(app, fn):
    with app.app_context():
        return fn()


def test_the_file_lock_blocks_another_process(tmp_path):
    import subprocess, sys
    path = tmp_path / "lock"
    lock = run_mod.RunLock(str(path))
    assert lock.acquire()
    code = ("import fcntl,sys\nf=open(sys.argv[1],'a+')\n"
            "try:\n fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB); print('got')\nexcept OSError: print('busy')")
    out = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True).stdout.strip()
    lock.release()
    assert out == "busy"
    assert lock.acquire()
    lock.release()


# ------------------------------------------------------------------ reconcile & prune

def _row(app_id, url, status, message=""):
    return {"applicationId": app_id, "url": url, "status": status, "statusLabel": status.lower(), "statusMessage": {"title": message} if message else None}


def test_earlier_in_flight_applications_are_brought_up_to_date(app):
    job = Job(source="tsenta", external_id="x", title="SWE", job_url="https://x.example/1", status="applying", applied_via="auto", tsenta_application_id="app-x")
    db.session.add(job)
    db.session.commit()
    client = FakeClient(True)
    client.rows_list = [_row("app-x", "https://x.example/1", "COMPLETED", "Submitted")]
    assert run_mod.reconcile(client) == 1
    assert db.session.get(Job, job.id).status == "applied"


def test_a_manual_apply_tsenta_says_failed_shows_up_as_failed(app):
    job = Job(source="simplify", external_id="m", title="SWE", job_url="https://m.example/job?utm_source=x", status="applied", applied_via="manual",
              status_message="Sent to Tsenta; it was still processing when last checked", status_changed_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    client = FakeClient(True)
    client.rows_list = [_row("app-m", "https://www.m.example/job", "FAILED", "Employer form rejected it")]
    assert run_mod.reconcile(client) == 1
    job = db.session.get(Job, job.id)
    assert job.status == "apply_failed" and "Employer form rejected it" in job.status_message and job.tsenta_application_id == "app-m"
    page = app.test_client().get("/history?status=apply_failed").data.decode()
    assert "Failed" in page and "m.example" in page


def test_a_confirmed_apply_keeps_its_status_and_gains_the_id(app):
    job = Job(source="simplify", external_id="ok", title="SWE", job_url="https://ok.example/1", status="applied", applied_via="manual",
              status_message="Submitted via Tsenta MCP", status_changed_at=datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    client = FakeClient(True)
    client.rows_list = [_row("app-ok", "https://ok.example/1", "COMPLETED", "Sent")]
    assert run_mod.reconcile(client) == 0
    job = db.session.get(Job, job.id)
    assert job.status == "applied" and job.status_message == "Submitted via Tsenta MCP" and job.tsenta_application_id == "app-ok"


def test_jobs_tsenta_has_no_record_of_are_left_alone_and_old_ones_are_not_checked(app):
    fresh = Job(source="simplify", external_id="n", title="SWE", job_url="https://n.example/1", status="applied", applied_via="manual", status_changed_at=datetime.utcnow())
    old = Job(source="simplify", external_id="o", title="SWE", job_url="https://o.example/1", status="applied", applied_via="manual",
              status_changed_at=datetime.utcnow() - timedelta(days=30))
    db.session.add_all([fresh, old])
    db.session.commit()
    client = FakeClient(True)
    client.rows_list = [_row("app-o", "https://o.example/1", "FAILED", "nope")]
    assert run_mod.reconcile(client) == 0
    assert db.session.get(Job, fresh.id).status == "applied" and db.session.get(Job, old.id).status == "applied"


def test_old_decisions_are_pruned(app):
    run = AutoApplyRun(dry_run=True)
    db.session.add(run)
    db.session.commit()
    db.session.add(AutoApplyDecision(run_id=run.id, verdict="skip", created_at=datetime.utcnow() - timedelta(days=30)))
    db.session.commit()
    go(app, FakeClient(True))
    assert AutoApplyDecision.query.filter(AutoApplyDecision.created_at < datetime.utcnow() - timedelta(days=20)).count() == 0


# ------------------------------------------------------------------ scheduler clock

NY = sched.SCHEDULE_TZ


@pytest.fixture
def clock(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(sched.schedule_state, "save", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(sched, "_last_fired_slot", None)
    return saved


def test_runs_every_day_from_6am_to_11pm(clock):
    assert sched._due_slot(datetime(2026, 11, 25, 6, 0, tzinfo=NY)) is not None
    assert sched._due_slot(datetime(2026, 11, 25, 23, 59, tzinfo=NY)) is not None
    assert sched._due_slot(datetime(2026, 11, 25, 5, 59, tzinfo=NY)) is None
    assert sched._due_slot(datetime(2026, 11, 25, 0, 30, tzinfo=NY)) is None
    assert sched._due_slot(datetime(2026, 11, 28, 12, 0, tzinfo=NY)) is not None  # Saturday
    assert sched._due_slot(datetime(2026, 11, 29, 12, 0, tzinfo=NY)) is not None  # Sunday


def test_each_hour_fires_once_and_a_missed_one_runs_late(app, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(sched, "_run_once", lambda a: calls.append(1) or object())
    sched.tick(app, datetime(2026, 11, 25, 10, 0, tzinfo=NY))
    sched.tick(app, datetime(2026, 11, 25, 10, 1, tzinfo=NY))
    assert len(calls) == 1
    sched.tick(app, datetime(2026, 11, 25, 11, 40, tzinfo=NY))                    # woke from sleep 40 minutes into the hour
    assert len(calls) == 2
    sched.tick(app, datetime(2026, 11, 28, 3, 0, tzinfo=NY))                     # 3am: not scheduled
    assert len(calls) == 2


def test_a_busy_slot_is_retried_not_lost(app, clock, monkeypatch):
    results = iter([None, object()])
    monkeypatch.setattr(sched, "_run_once", lambda a: next(results))
    assert sched.tick(app, datetime(2026, 11, 25, 10, 0, tzinfo=NY)) is None
    assert sched.tick(app, datetime(2026, 11, 25, 10, 1, tzinfo=NY)) is not None


def test_the_daylight_saving_switch_is_handled(app, clock):
    assert sched._due_slot(datetime(2026, 11, 2, 9, 0, tzinfo=NY)) is not None     # first Monday after DST ended
    assert datetime(2026, 11, 2, 9, 0, tzinfo=NY).utcoffset() == timedelta(hours=-5)


# ------------------------------------------------------------------ web pages & migration

def test_runs_pages_show_pass_and_skip_with_reasons(app):
    run = go(app, FakeClient(True, rows=[feed_row(1), feed_row(2, title="Chef")]))
    client = app.test_client()
    assert b"DRY RUN" in client.get("/history/auto/runs").data
    page = client.get(f"/history/auto/runs/{run.id}").data.decode()
    assert "Pass" in page and "Skip" in page and "title" in page and "Chef" in page
    assert "Chef" not in client.get(f"/history/auto/runs/{run.id}?verdict=pass").data.decode()
    assert client.get("/history/auto/runs/9999").status_code == 404


def test_auto_history_lists_feed_jobs_and_needs_you(app):
    db.session.add(Job(source="tsenta", external_id="a", title="SWE", company="Acme", job_url="https://acme.example/a", status="needs_review",
                       status_message="needs a document", applied_via="auto", status_changed_at=datetime.utcnow()))
    db.session.commit()
    page = app.test_client().get("/history/auto").data.decode()
    assert "Needs you" in page and "https://acme.example/a" in page
    assert app.test_client().get("/history/auto?status=needs_review").status_code == 200


def test_upgrade_adds_columns_to_an_old_database_and_is_repeatable(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    import db_upgrade
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source VARCHAR(50), title VARCHAR(500))")
    con.execute("INSERT INTO jobs (source, title) VALUES ('simplify', 'keep me')")
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{path}")
    added = db_upgrade.upgrade(engine)
    assert set(added) == {f"jobs.{c}" for c in db_upgrade.NEW_COLUMNS["jobs"]}
    assert db_upgrade.upgrade(engine) == []
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT title, tsenta_job_id FROM jobs").fetchall() == [("keep me", None)]


def test_the_plist_carries_dry_run_and_hides_the_token():
    import os, subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def plist(**env):
        return subprocess.run([os.path.join(here, "autoapply"), "plist"], capture_output=True, text=True, env={**os.environ, **env, "PATH": os.environ["PATH"]})
    base = {k: v for k, v in os.environ.items() if k not in ("DRY_RUN", "TSENTA_MCP_TOKEN")}
    for value, expected in ((None, "true"), ("false", "false"), ("OFF", "false"), ("maybe", "true"), ("true", "true")):
        env = dict(base, **({"DRY_RUN": value} if value else {}))
        out = subprocess.run([os.path.join(here, "autoapply"), "plist"], capture_output=True, text=True, env=env).stdout
        assert f"<key>DRY_RUN</key>\n        <string>{expected}</string>" in out, (value, out)
        assert "TSENTA_MCP_TOKEN" not in out
    out = subprocess.run([os.path.join(here, "autoapply"), "plist"], capture_output=True, text=True, env=dict(base, TSENTA_MCP_TOKEN="s3cret")).stdout
    assert "TSENTA_MCP_TOKEN" in out and "s3cret" not in out
