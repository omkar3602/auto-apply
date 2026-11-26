"""The real wiring - session thread, the SDK's streaming HTTP transport, bearer auth - against a
fake Tsenta MCP server running on localhost. Nothing here touches the real Tsenta."""
import json
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from automation import tsenta_mcp as tm
from automation import tsenta_oauth as oauth

TOKEN = "test-token"


def env(tool, result):
    return json.dumps({"tool": tool, "result": result})


def job(job_id, title="Software Engineer"):
    return {"id": job_id, "title": title, "companyName": "Acme", "url": f"https://acme.example/{job_id}", "locations": ["New York, NY"],
            "datePosted": "2026-11-24T10:00:00.000Z", "yearsOfExperienceMin": 2, "sponsorship": "UNKNOWN", "matchScore": 80}


def build_app(state):
    server = MCPServer("fake-tsenta")

    @server.tool(name="get-application-balance")
    def balance() -> str:
        return env("get-application-balance", {"monthly": {"used": 1, "limit": 10, "remaining": state["credits"], "cycleEnd": "2026-12-18T00:00:00Z"},
                                               "total": state["credits"], "isAdminBypass": False})

    @server.tool(name="get-job-recommendations")
    def recommendations(search: str = "", page: int = 1, limit: int = 50, locations: list[str] | None = None, datePosted: str = "7d",
                        needsSponsorship: bool | None = None, excludeSearch: str | None = None) -> str:
        state["feed_calls"].append({"search": search, "page": page, "locations": locations, "datePosted": datePosted,
                                    "needsSponsorship": needsSponsorship, "excludeSearch": excludeSearch})
        pages = {"country:US": {1: ([job("a"), job("b")], True), 2: ([job("c")], False)}, "state:NY": {1: ([job("c"), job("d")], False)}}
        jobs, more = pages[locations[0]].get(page, ([], False))
        return env("get-job-recommendations", {"jobs": jobs, "pagination": {"page": page, "limit": limit}, "hasMore": more})

    @server.tool(name="get-jobs-by-ids")
    def jobs_by_ids(jobIds: list[str], descriptionMaxChars: int = 1500) -> str:
        state["desc_calls"].append(list(jobIds))
        return env("get-jobs-by-ids", {"jobs": [{"id": i, "description": f"<p>About {i}</p><ul><li>Python</li></ul>"} for i in jobIds],
                                       "requested": len(jobIds), "returned": len(jobIds)})

    @server.tool(name="fetch-job-description")
    def fetch(url: str) -> str:
        return env("fetch-job-description", {"status": "success", "jobDescription": f"Posting at {url}"})

    @server.tool(name="apply-to-job")
    def apply(jobId: str | None = None, jobIds: list[str] | None = None, url: str | None = None, jobDescription: str | None = None) -> str:
        state["apply_calls"].append({"jobId": jobId, "jobIds": jobIds, "url": url, "jobDescription": jobDescription})
        if state["credits"] <= 0:
            return env("apply-to-job", {"success": False, "error": "out of applications: this account has none left. Call get-application-balance"})
        refs = jobIds or ([jobId] if jobId else [url])
        results = []
        for ref in refs:
            state["credits"] -= 1
            results.append({"jobId": ref, "success": True, "applicationId": f"app-{ref}", "status": "OPTIMIZING_RESUME"})
        return env("apply-to-job", {"results": results, "remainingBalance": state["credits"]})

    @server.tool(name="list-applications")
    def list_apps(filter: str = "all", limit: int = 25, page: int = 1, includeCounts: bool = True, query: str | None = None) -> str:
        state["list_calls"] += 1
        status = "COMPLETED" if state["list_calls"] >= 2 else "RUNNING"          # settles on the second look
        rows = [{"applicationId": a, "status": status, "statusLabel": "sent" if status == "COMPLETED" else "working on it", "url": f"https://acme.example/{a[4:]}"}
                for a in state["applied"]]
        return env("list-applications", {"applications": rows, "pagination": {"page": page, "limit": limit, "total": len(rows)}})

    app = server.streamable_http_app()

    async def guarded(scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != f"Bearer {TOKEN}".encode():
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json"),
                                                                                 (b"www-authenticate", b'Bearer error="invalid_token"')]})
            await send({"type": "http.response.body", "body": b'{"error":"invalid_token"}'})
            return
        await app(scope, receive, send)

    return guarded


@pytest.fixture
def fake_tsenta():
    state = {"credits": 5, "feed_calls": [], "desc_calls": [], "apply_calls": [], "list_calls": 0, "applied": []}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build_app(state), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "fake server didn't start"
    state["url"] = f"http://127.0.0.1:{port}/mcp"
    yield state
    server.should_exit = True
    thread.join(timeout=10)


def client_for(state, **kw):
    return tm.TsentaClient(url=state["url"], token=kw.pop("token", TOKEN), oauth_file="/nonexistent/none.json", call_timeout=30, **kw)


def test_balance_and_feed_over_the_real_transport(fake_tsenta):
    with client_for(fake_tsenta) as tsenta:
        balance = tsenta.balance()
        assert balance.total == 5 and balance.monthly_remaining == 5 and balance.cycle_end == "2026-12-18T00:00:00Z" and balance.has_room
        jobs = tsenta.feed()
    assert [j["id"] for j in jobs] == ["a", "b", "c", "d"]                          # both pages, both location sets, "c" once
    calls = fake_tsenta["feed_calls"]
    assert [(c["locations"], c["page"]) for c in calls] == [(["country:US"], 1), (["country:US"], 2), (["state:NY"], 1)]
    assert all(c["needsSponsorship"] is True and c["datePosted"] == "24h" for c in calls)
    assert "Software Engineer" in calls[0]["search"] and "Forward Deployed Engineer" in calls[0]["search"] and calls[0]["excludeSearch"].startswith("Staff|")


def test_descriptions_are_converted_to_text(fake_tsenta):
    with client_for(fake_tsenta) as tsenta:
        text = tsenta.descriptions(["a", "b"])
    assert text["a"].startswith("About a") and "- Python" in text["a"] and "<" not in text["a"]


def test_applying_confirming_and_a_clean_shutdown(fake_tsenta):
    before = {t.name for t in threading.enumerate()}
    with client_for(fake_tsenta, allow_apply=True) as tsenta:
        results = tsenta.apply_jobs(["a", "b"])
        assert [r.success for r in results] == [True, True] and results[0].application_id == "app-a"
        fake_tsenta["applied"] = [r.application_id for r in results]
        outcomes = tsenta.wait_for_outcomes([r.application_id for r in results], timeout_s=30, poll_s=0.05)
        assert {o.kind for o in outcomes.values()} == {"applied"}
    assert fake_tsenta["apply_calls"][0]["jobIds"] == ["a", "b"] and fake_tsenta["credits"] == 3
    time.sleep(0.3)
    assert "tsenta-mcp" not in {t.name for t in threading.enumerate()}              # our session thread is gone


def test_a_dry_run_client_never_reaches_apply_even_by_accident(fake_tsenta):
    with client_for(fake_tsenta, allow_apply=False) as tsenta:
        with pytest.raises(tm.ApplyBlocked):
            tsenta.apply_jobs(["a"])
        with pytest.raises(tm.ApplyBlocked):
            tsenta.apply_url("https://acme.example/x")
    assert fake_tsenta["apply_calls"] == []


def test_running_out_of_credits_is_reported_and_stops_the_rest(fake_tsenta):
    fake_tsenta["credits"] = 0
    with client_for(fake_tsenta, allow_apply=True) as tsenta:
        results = tsenta.apply_jobs([f"j{i}" for i in range(25)])
    assert len(fake_tsenta["apply_calls"]) == 1                                     # stopped after the first chunk
    assert all(r.category == "out_of_balance" and not r.success for r in results) and len(results) == 25
    assert any("not attempted" in r.message for r in results)


def test_apply_one_url_end_to_end(fake_tsenta):
    fake_tsenta["applied"] = ["app-https://acme.example/z"]                          # what the fake will list back
    client = client_for(fake_tsenta, allow_apply=True)
    with client:
        ok, message = tm.apply_one_url("https://acme.example/z", client=client, outcome_timeout_s=30)
    assert ok and "Submitted via Tsenta MCP" in message
    assert fake_tsenta["apply_calls"][0]["jobDescription"] == "Posting at https://acme.example/z"


def test_a_rejected_token_raises_needs_login(fake_tsenta):
    with pytest.raises(oauth.NeedsLogin):
        with client_for(fake_tsenta, token="wrong-token") as tsenta:
            tsenta.balance()
