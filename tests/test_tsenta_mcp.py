"""Parsers and client logic, with the transport replaced by a fake. Fixture rows are real
list-applications / get-application-balance shapes captured from a live account (2026-11-25),
with ids and names changed."""
import types

import pytest

from automation import tsenta_mcp as tm
from automation import tsenta_oauth as oauth

BALANCE = {"free": {"used": 25, "limit": 0, "remaining": 0}, "monthly": {"used": 162, "limit": 1500, "remaining": 1338,
           "cycleStart": "2026-11-18T18:41:37.549Z", "cycleEnd": "2026-12-18T18:41:37.549Z"}, "pack": {"used": 400, "lifetime": 400, "remaining": 0},
           "total": 1338, "isAdminBypass": False}

ROW_FAILED_RETRY = {"applicationId": "app-1", "jobId": "run-1", "jobStatus": "FAILED", "errorCode": "UNKNOWN", "canReapply": False,
                    "statusMessage": {"title": "Application Failed", "description": "We hit an unexpected error submitting this application. It will be retried automatically.", "tier": "error"},
                    "url": "https://acme.applytojob.com/apply/x/Junior-AI-Engineer", "companyName": "Acme", "jobTitle": "Junior AI Engineer", "status": "FAILED", "statusLabel": "couldn't send"}
ROW_FAILED_SCREENER = {"applicationId": "app-2", "status": "FAILED", "errorCode": "INDEED_SCREENER_REJECTED", "canReapply": False,
                       "statusMessage": {"title": "Indeed Screener Answer Rejected", "description": "Indeed rejected one or more employer screener answers before submission.", "tier": "warning"},
                       "statusLabel": "couldn't send"}
ROW_SENT = {"applicationId": "app-3", "status": "COMPLETED", "statusMessage": None, "statusLabel": "sent", "url": "https://x.oraclecloud.com/job/1", "companyName": "Dell", "jobTitle": "Analyst"}
ROW_NEEDS = {"applicationId": "app-4", "jobStatus": "RUNNING", "status": "PENDING_SUBMIT_REVIEW", "statusMessage": None, "readyToSend": False,
             "statusLabel": "needs you: the form is filled and waits for your review before it sends",
             "missingRequired": [{"fieldId": "state", "label": "State"}, {"fieldId": "visa", "label": "Will you require sponsorship?"}]}
ROW_RUNNING = {"applicationId": "app-5", "status": "OPTIMIZING_RESUME", "statusLabel": "working on it"}


# ------------------------------------------------------------------ parsers

def test_parse_balance():
    b = tm.parse_balance(BALANCE)
    assert (b.total, b.monthly_remaining, b.monthly_limit, b.cycle_end, b.admin_bypass) == (1338, 1338, 1500, "2026-12-18T18:41:37.549Z", False) and b.has_room
    assert not tm.parse_balance({"total": 0, "monthly": {"remaining": 0}}).has_room
    assert tm.parse_balance({"total": 0, "isAdminBypass": True}).has_room
    assert tm.parse_balance({}).total == 0


@pytest.mark.parametrize("row,kind,fragment", [
    (ROW_SENT, "applied", ""), (ROW_FAILED_RETRY, "failed", "retried automatically"), (ROW_FAILED_SCREENER, "failed", "Screener Answer Rejected"),
    (ROW_NEEDS, "needs_you", "State"), (ROW_RUNNING, "pending", ""),
    ({"applicationId": "s", "status": "SKIPPED"}, "failed", "Skipped"),
    ({"applicationId": "r", "status": "READY"}, "pending", ""), ({"applicationId": "u"}, "pending", ""),
])
def test_outcome_kinds(row, kind, fragment):
    outcome = tm.outcome_from_row(row)
    assert outcome.kind == kind and fragment in outcome.message


def test_outcome_carries_tsentas_own_wording_and_flags():
    o = tm.outcome_from_row(ROW_FAILED_RETRY)
    assert o.message == "Application Failed - We hit an unexpected error submitting this application. It will be retried automatically."
    assert o.will_retry and not o.can_reapply and o.error_code == "UNKNOWN" and o.company == "Acme" and o.title == "Junior AI Engineer"
    assert not tm.outcome_from_row(ROW_FAILED_SCREENER).will_retry
    needs = tm.outcome_from_row(ROW_NEEDS)
    assert needs.missing == ["State", "Will you require sponsorship?"] and "needs:" in needs.message


@pytest.mark.parametrize("message,category", [
    ("out of applications: this account has none left. Call get-application-balance", "out_of_balance"),
    ("profile is missing: phone number", "profile_missing"), ("already applied to this job", "already_applied"),
    ("that job posting has been closed", "closed"), ("we don't support that application system yet", "unsupported"),
    ("something else broke", "error"), ("", "error"), (None, "error"),
])
def test_classify_apply_error(message, category):
    assert tm.classify_apply_error(message) == category


def test_parse_apply_response_by_id_position_and_single_object():
    by_id = {"results": [{"jobId": "b", "success": False, "error": "already applied to this job"}, {"jobId": "a", "success": True, "applicationId": "app-a", "status": "OPTIMIZING_RESUME"}]}
    a, b = tm.parse_apply_response(by_id, ["a", "b"])
    assert (a.success, a.application_id, a.status, a.category) == (True, "app-a", "OPTIMIZING_RESUME", "applied")
    assert (b.success, b.category, b.message) == (False, "already_applied", "already applied to this job")
    positional = {"results": [{"success": True, "applicationId": "x"}, {"success": False, "message": "that job posting has been closed"}]}
    first, second = tm.parse_apply_response(positional, ["j1", "j2"])
    assert first.application_id == "x" and second.category == "closed"
    single = tm.parse_apply_response({"success": True, "applicationId": "only"}, ["https://x"])
    assert single[0].success and single[0].ref == "https://x" and single[0].application_id == "only"


def test_parse_apply_response_whole_call_refused_or_missing():
    refused = tm.parse_apply_response({"success": False, "error": "out of applications: this account has none left"}, ["a", "b"])
    assert [r.category for r in refused] == ["out_of_balance", "out_of_balance"] and all(r.stop_run for r in refused)
    missing = tm.parse_apply_response({"results": [{"jobId": "a", "success": True}]}, ["a", "b"])
    assert missing[1].category == "unknown" and not missing[1].success and "check" in missing[1].message
    assert tm.parse_apply_response("garbage", ["a"])[0].category == "unknown"


def test_html_to_text():
    html = "<p>DATA ENGINEER</p><p>Location: Charlotte &amp; remote</p><ul><li>Python</li><li>SQL</li></ul><br>Apply&nbsp;now"
    text = tm.html_to_text(html)
    assert "DATA ENGINEER\nLocation: Charlotte & remote" in text and "- Python" in text and "- SQL" in text and "<" not in text
    assert tm.html_to_text(None) == "" and tm.html_to_text("") == ""


def result(content=None, structured=None, is_error=False):
    items = [types.SimpleNamespace(text=t) for t in (content or [])]
    return types.SimpleNamespace(content=items, structured_content=structured, is_error=is_error)


def test_payload_prefers_the_json_text_and_unwraps_result():
    text = '{"tool": "get-application-balance", "result": {"total": 7}}'
    assert tm._payload("t", result([text], structured={"result": "wrapped string"})) == {"total": 7}
    assert tm._payload("t", result(['{"jobs": []}'])) == {"jobs": []}                        # no "result" wrapper
    assert tm._payload("t", result(["not json"])) == {"text": "not json"}
    assert tm._payload("t", result([], structured={"a": 1})) == {"a": 1}
    assert tm._payload("t", result([])) == {}


def test_payload_raises_a_tool_error_with_the_servers_words():
    with pytest.raises(tm.TsentaToolError, match="out of applications"):
        tm._payload("apply-to-job", result(['{"error": "out of applications: none left"}'], is_error=True))
    with pytest.raises(tm.TsentaToolError, match="boom"):
        tm._payload("x", result(["boom"], is_error=True))


# ------------------------------------------------------------------ client logic with a fake _call

class Fake:
    """Stands in for TsentaClient._call and records every call."""

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def __call__(self, name, args, *, retry=True):
        self.calls.append((name, args, retry))
        reply = self.replies[name]
        reply = reply(args) if callable(reply) else reply
        if isinstance(reply, Exception):
            raise reply
        return reply


def client(replies, *, allow_apply=True):
    c = tm.TsentaClient(token="t", allow_apply=allow_apply)
    c._call = Fake(replies)
    c._sleep = lambda s: None
    return c


def test_feed_pages_each_location_set_and_dedupes():
    pages = {("country:US", 1): {"jobs": [{"id": "a"}, {"id": "b"}], "hasMore": True}, ("country:US", 2): {"jobs": [{"id": "c"}], "hasMore": False},
             ("state:NY", 1): {"jobs": [{"id": "b"}, {"id": "d"}], "hasMore": False}}
    c = client({"get-job-recommendations": lambda args: pages[(args["locations"][0], args["page"])]})
    assert [j["id"] for j in c.feed()] == ["a", "b", "c", "d"]
    sent = [a for _, a, _ in c._call.calls]
    assert [(a["locations"], a["page"]) for a in sent] == [(["country:US"], 1), (["country:US"], 2), (["state:NY"], 1)]
    assert all(a["limit"] == 50 and a["datePosted"] == "24h" and a["needsSponsorship"] is True and a["excludeSearch"].startswith("Staff|") for a in sent)
    assert "jobTypes" not in sent[0]                                        # no ENTRY_LEVEL restriction


def test_feed_notes_odd_replies_and_raises_when_the_feed_is_down():
    narrow = client({"get-job-recommendations": {"jobs": [], "hasMore": False, "zeroResultReason": "FILTERS_NARROW"}})
    assert narrow.feed() == [] and any("FILTERS_NARROW" in n for n in narrow.last_feed_notes)
    down = client({"get-job-recommendations": {"jobs": [], "zeroResultReason": "TEMPORARILY_UNAVAILABLE"}})
    with pytest.raises(tm.TsentaError, match="TEMPORARILY_UNAVAILABLE"):
        down.feed()
    no_profile = client({"get-job-recommendations": {"jobs": [], "zeroResultReason": "NO_PROFILE"}})
    with pytest.raises(tm.TsentaError):
        no_profile.feed()


def test_a_temporarily_unavailable_page_is_retried_then_succeeds():
    attempts = {"n": 0}

    def reply(args):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return {"jobs": [], "zeroResultReason": "TEMPORARILY_UNAVAILABLE"}
        return {"jobs": [{"id": "a"}], "hasMore": False}
    c = client({"get-job-recommendations": reply})
    assert [j["id"] for j in c.feed(location_sets=(["country:US"],))] == ["a"] and attempts["n"] == 3


def test_one_failing_search_keeps_what_the_others_fetched():
    def reply(args):
        if args["locations"] == ["state:NY"]:
            return {"jobs": [], "zeroResultReason": "TEMPORARILY_UNAVAILABLE"}
        return {"jobs": [{"id": f"us{args['page']}"}], "hasMore": args["page"] < 2}
    c = client({"get-job-recommendations": reply})
    assert [j["id"] for j in c.feed()] == ["us1", "us2"]                             # the US pages survive the NY failure
    assert any("state:NY" in n and "failed after 3 tries" in n and "kept the 2 job(s)" in n for n in c.last_feed_notes)


def test_a_page_failing_midway_keeps_the_earlier_pages():
    def reply(args):
        if args["page"] == 2:
            return tm.TsentaError("connection dropped")
        return {"jobs": [{"id": "p1"}], "hasMore": True}
    c = client({"get-job-recommendations": reply})
    assert [j["id"] for j in c.feed(location_sets=(["country:US"],))] == ["p1"]


def test_a_sign_in_problem_is_never_swallowed_by_the_feed():
    c = client({"get-job-recommendations": tm.NeedsLogin("expired")})
    with pytest.raises(tm.NeedsLogin):
        c.feed()


def test_feed_stops_at_max_pages():
    c = client({"get-job-recommendations": lambda a: {"jobs": [{"id": f"j{a['page']}"}], "hasMore": True}})
    jobs = c.feed(max_pages=3, location_sets=(["country:US"],))
    assert len(jobs) == 3 and any("stopped at 3 pages" in n for n in c.last_feed_notes)


def test_descriptions_batch_to_the_servers_character_budget():
    c = client({"get-jobs-by-ids": lambda a: {"jobs": [{"id": i, "description": f"<p>{i}</p>"} for i in a["jobIds"]]}})
    out = c.descriptions([f"j{i}" for i in range(20)], max_chars=6000)
    assert [len(a["jobIds"]) for _, a, _ in c._call.calls] == [8, 8, 4]       # 48,000 // 6,000 per call
    assert all(a["descriptionMaxChars"] == 6000 for _, a, _ in c._call.calls) and out["j3"] == "j3"
    c2 = client({"get-jobs-by-ids": {"jobs": []}})
    c2.descriptions(["a", "a", "", "b"], max_chars=1000)
    assert c2._call.calls[0][1]["jobIds"] == ["a", "b"]                         # de-duplicated, blanks dropped


def test_fetch_job_description_returns_status_and_text():
    c = client({"fetch-job-description": {"status": "expired", "jobDescription": None}})
    assert c.fetch_job_description("https://x") == ("expired", None)


def test_apply_is_blocked_when_applying_is_off_and_never_reaches_the_server():
    c = client({}, allow_apply=False)
    with pytest.raises(tm.ApplyBlocked):
        c.apply_jobs(["a"])
    with pytest.raises(tm.ApplyBlocked):
        c.apply_url("https://x")
    assert c._call.calls == []


def test_apply_jobs_chunks_by_ten_uses_jobid_for_a_single_job_and_is_never_retried():
    replies = {"apply-to-job": lambda a: {"results": [{"jobId": j, "success": True, "applicationId": f"app-{j}"} for j in (a.get("jobIds") or [a["jobId"]])]}}
    c = client(replies)
    results = c.apply_jobs([f"j{i}" for i in range(21)])
    assert len(results) == 21 and all(r.success for r in results)
    assert [len(a.get("jobIds") or [a["jobId"]]) for _, a, _ in c._call.calls] == [10, 10, 1]
    assert "jobId" in c._call.calls[2][1] and "jobIds" not in c._call.calls[2][1]
    assert all(retry is False for _, _, retry in c._call.calls)


def test_apply_jobs_stops_after_an_out_of_credits_chunk():
    c = client({"apply-to-job": {"success": False, "error": "out of applications: this account has none left"}})
    results = c.apply_jobs([f"j{i}" for i in range(25)])
    assert len(c._call.calls) == 1 and len(results) == 25 and all(r.category == "out_of_balance" for r in results)
    assert results[-1].message.startswith("not attempted")


def test_an_unclear_apply_result_is_reported_not_retried_and_login_problems_propagate():
    unclear = client({"apply-to-job": tm.TsentaError("timed out")})
    result = unclear.apply_url("https://x")
    assert result.category == "unknown" and not result.success and "before retrying" in result.message
    assert len(unclear._call.calls) == 1
    refused = client({"apply-to-job": tm.TsentaToolError("apply-to-job: that job posting has been closed")})
    assert refused.apply_url("https://x").category == "closed"
    expired = client({"apply-to-job": oauth.NeedsLogin("expired")})
    with pytest.raises(oauth.NeedsLogin):
        expired.apply_url("https://x")


def test_apply_url_sends_the_description_trimmed():
    c = client({"apply-to-job": {"success": True, "applicationId": "z"}})
    c.apply_url("https://x", "d" * 20_000)
    assert c._call.calls[0][1]["url"] == "https://x" and len(c._call.calls[0][1]["jobDescription"]) == 10_000
    c.apply_url("https://y")
    assert "jobDescription" not in c._call.calls[1][1]


def test_wait_for_outcomes_polls_until_settled():
    seen = {"n": 0}

    def listing(args):
        seen["n"] += 1
        status = "COMPLETED" if seen["n"] >= 3 else "OPTIMIZING_RESUME"
        return {"applications": [{**ROW_SENT, "applicationId": "a1", "status": status}, {**ROW_FAILED_SCREENER, "applicationId": "a2"}]}
    c = client({"list-applications": listing})
    out = c.wait_for_outcomes(["a1", "a2"], timeout_s=60, poll_s=0)
    assert out["a1"].kind == "applied" and out["a2"].kind == "failed" and seen["n"] == 3


def test_wait_for_outcomes_gives_up_and_leaves_it_pending():
    c = client({"list-applications": {"applications": [{**ROW_RUNNING, "applicationId": "a1"}]}})
    out = c.wait_for_outcomes(["a1", "missing"], timeout_s=0, poll_s=0)
    assert out["a1"].kind == "pending" and "missing" not in out


def test_wait_for_outcomes_reads_further_pages_when_needed():
    def listing(args):
        if args["page"] == 1:
            return {"applications": [{"applicationId": f"x{i}", "status": "COMPLETED"} for i in range(100)]}
        return {"applications": [{**ROW_SENT, "applicationId": "late"}]}
    c = client({"list-applications": listing})
    assert c.wait_for_outcomes(["late"], timeout_s=5, poll_s=0)["late"].kind == "applied"
    assert [a["page"] for _, a, _ in c._call.calls] == [1, 2]


# ------------------------------------------------------------------ the retry rules in _call

class FlakySession:
    def __init__(self, behaviours):
        self.behaviours, self.calls = list(behaviours), 0

    def call(self, name, args, timeout):
        self.calls += 1
        step = self.behaviours.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def with_session(behaviours):
    c = tm.TsentaClient(token="t")
    c._session = FlakySession(behaviours)
    c.reconnects = 0

    def reconnect():
        c.reconnects += 1
        c._session = FlakySession(c._session.behaviours) if c._session else FlakySession([])
    c._disconnect = lambda: setattr(c, "_session", types.SimpleNamespace(behaviours=c._session.behaviours))
    c._connect = reconnect
    return c


def test_a_read_reconnects_and_retries_once():
    c = with_session([RuntimeError("socket closed"), {"ok": 1}])
    assert c._call("get-application-balance", {}) == {"ok": 1} and c.reconnects == 1


def test_a_read_that_keeps_failing_raises_after_one_retry():
    c = with_session([RuntimeError("a"), RuntimeError("b")])
    with pytest.raises(tm.TsentaError, match="get-application-balance failed"):
        c._call("get-application-balance", {})


def test_no_retry_when_retry_is_off_and_tool_errors_pass_straight_through():
    c = with_session([RuntimeError("socket closed"), {"ok": 1}])
    with pytest.raises(tm.TsentaError):
        c._call("apply-to-job", {}, retry=False)
    assert c.reconnects == 0
    c2 = with_session([tm.TsentaToolError("nope")])
    with pytest.raises(tm.TsentaToolError):
        c2._call("x", {})
    assert c2.reconnects == 0


def test_a_login_problem_is_not_retried():
    c = with_session([oauth.NeedsLogin("expired"), {"ok": 1}])
    with pytest.raises(oauth.NeedsLogin):
        c._call("x", {})
    assert c.reconnects == 0


def test_calling_without_a_session_explains_itself():
    with pytest.raises(tm.TsentaError, match="not connected"):
        tm.TsentaClient(token="t")._call("x", {})


# ------------------------------------------------------------------ configuration

def test_is_configured_and_from_env(monkeypatch, tmp_path):
    path = tmp_path / "tsenta_oauth.json"
    monkeypatch.delenv("TSENTA_MCP_TOKEN", raising=False)
    assert not tm.is_configured(path)
    with pytest.raises(tm.NotConfigured, match="tsenta_login.py"):
        tm.TsentaClient.from_env(oauth_file=path)
    monkeypatch.setenv("TSENTA_MCP_TOKEN", "  abc  ")
    assert tm.is_configured(path) and tm.static_token() == "abc"
    assert tm.TsentaClient.from_env(oauth_file=path).token == "abc"
    monkeypatch.delenv("TSENTA_MCP_TOKEN")
    import asyncio
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
    storage = oauth.FileTokenStorage(path)
    asyncio.run(storage.set_client_info(OAuthClientInformationFull(client_id="c", redirect_uris=["http://127.0.0.1:1/cb"])))
    asyncio.run(storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer", refresh_token="r")))
    assert tm.is_configured(path)
    assert tm.TsentaClient.from_env(oauth_file=path).token is None


# ------------------------------------------------------------------ apply_one_url

class FakeOne:
    def __init__(self, *, balance=None, fetch=("success", "desc"), apply=None, outcome=None):
        self.balance_value = balance or tm.parse_balance(BALANCE)
        self.fetch_value, self.apply_value, self.outcome_value = fetch, apply, outcome
        self.applied = []

    def balance(self):
        return self.balance_value

    def fetch_job_description(self, url):
        if isinstance(self.fetch_value, Exception):
            raise self.fetch_value
        return self.fetch_value

    def apply_url(self, url, description=None):
        self.applied.append((url, description))
        return self.apply_value

    def wait_for_outcomes(self, ids, timeout_s=0, poll_s=0):
        return {ids[0]: self.outcome_value} if self.outcome_value else {}


def ok_apply(app_id="app-1"):
    return tm.ApplyResult("https://x", True, "applied", application_id=app_id)


def test_apply_one_url_confirmed_sent():
    f = FakeOne(apply=ok_apply(), outcome=tm.outcome_from_row(ROW_SENT))
    ok, message = tm.apply_one_url("https://x", client=f)
    assert ok and "Submitted via Tsenta MCP" in message and f.applied == [("https://x", "desc")]


@pytest.mark.parametrize("scenario,expect_ok,fragment", [
    (dict(balance=tm.Balance(0, 0, 1500, "2026-12-18", False)), False, "no applications left"),
    (dict(fetch=("expired", None)), False, "closed"),
    (dict(apply=tm.ApplyResult("https://x", False, "unsupported", "we don't support that application system yet")), False, "unsupported"),
    (dict(apply=ok_apply(None)), True, "not confirmed"),
    (dict(apply=ok_apply(), outcome=None), True, "still processing"),
    (dict(apply=ok_apply(), outcome=tm.outcome_from_row(ROW_RUNNING)), True, "still processing"),
    (dict(apply=ok_apply(), outcome=tm.outcome_from_row(ROW_NEEDS)), False, "needs: State"),
    (dict(apply=ok_apply(), outcome=tm.outcome_from_row(ROW_FAILED_RETRY)), False, "retry it by itself"),
    (dict(apply=ok_apply(), outcome=tm.outcome_from_row(ROW_FAILED_SCREENER)), False, "Screener"),
])
def test_apply_one_url_scenarios(scenario, expect_ok, fragment):
    scenario.setdefault("apply", ok_apply())
    ok, message = tm.apply_one_url("https://x", client=FakeOne(**scenario))
    assert ok is expect_ok and fragment in message, message


def test_apply_one_url_applies_without_a_description_when_fetching_fails():
    f = FakeOne(fetch=tm.TsentaError("down"), apply=ok_apply(), outcome=tm.outcome_from_row(ROW_SENT))
    assert tm.apply_one_url("https://x", client=f)[0] and f.applied == [("https://x", None)]


def test_apply_one_url_does_nothing_when_there_are_no_credits():
    f = FakeOne(balance=tm.Balance(0, 0, 1500, None, False))
    tm.apply_one_url("https://x", client=f)
    assert f.applied == []


def test_pagination_exceeded_after_results_is_not_reported():
    def reply(args):
        if args["page"] == 1:
            return {"jobs": [{"id": "a"}], "hasMore": True}
        return {"jobs": [], "hasMore": False, "zeroResultReason": "PAGINATION_EXCEEDED"}
    c = client({"get-job-recommendations": reply})
    assert [j["id"] for j in c.feed(location_sets=(["country:US"],))] == ["a"]
    assert c.last_feed_notes == []
    empty = client({"get-job-recommendations": {"jobs": [], "hasMore": False, "zeroResultReason": "PAGINATION_EXCEEDED"}})
    assert empty.feed(location_sets=(["country:US"],)) == [] and any("PAGINATION_EXCEEDED" in n for n in empty.last_feed_notes)
