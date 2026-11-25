"""The one-time login helper, end to end against a fake Tsenta that implements OAuth 2.1 + PKCE
(discovery, dynamic registration, authorize, token, refresh) and an MCP endpoint. Also the
dispatcher that decides between the browser flow and the MCP client."""
import base64
import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

import config as cfg
from automation import tsenta
from automation import tsenta_mcp as tm
from automation import tsenta_oauth as oauth
from tools import tsenta_login as login_tool


# ------------------------------------------------------------------ callback server

def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_callback_server_catches_the_code():
    server = login_tool.CallbackServer()
    try:
        assert server.redirect_uri == f"http://127.0.0.1:{server.port}/callback"
        status, body = get(f"{server.redirect_uri}?code=abc&state=xyz&iss=https%3A%2F%2Fapi")
        assert status == 200 and b"Signed in" in body
        assert server.wait(2) == {"code": "abc", "state": "xyz", "iss": "https://api"}
    finally:
        server.close()


def test_callback_server_reports_a_refusal_and_ignores_other_paths():
    server = login_tool.CallbackServer()
    try:
        assert get(f"http://127.0.0.1:{server.port}/favicon.ico")[0] == 404
        assert get(f"{server.redirect_uri}?error=access_denied")[0] == 400
        assert server.wait(2)["error"] == "access_denied"
    finally:
        server.close()


def test_callback_server_times_out():
    server = login_tool.CallbackServer()
    try:
        with pytest.raises(TimeoutError):
            server.wait(0.1)
    finally:
        server.close()


# ------------------------------------------------------------------ a fake Tsenta with real OAuth

class FakeTsenta:
    def __init__(self, base):
        self.base = base
        self.access_ttl = 1
        self.valid_access = set()
        self.valid_refresh = set()
        self.codes = {}                  # code -> (challenge, redirect_uri)
        self.registered = {}
        self.deny = False
        self.revoke_refresh = False
        self.log = []
        self.issued = 0

    def _issue(self):
        self.issued += 1
        access, refresh = f"access-{self.issued}", f"refresh-{self.issued}"
        self.valid_access = {access}
        self.valid_refresh = {refresh}
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.access_ttl, "refresh_token": refresh}

    async def prm(self, request):
        return JSONResponse({"resource": f"{self.base}/mcp", "authorization_servers": [self.base]})

    async def metadata(self, request):
        return JSONResponse({"issuer": self.base, "authorization_endpoint": f"{self.base}/oauth/authorize", "token_endpoint": f"{self.base}/oauth/token",
                             "registration_endpoint": f"{self.base}/oauth/register", "response_types_supported": ["code"],
                             "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
                             "token_endpoint_auth_methods_supported": ["none"]})

    async def register(self, request):
        body = await request.json()
        client_id = f"client-{len(self.registered) + 1}"
        self.registered[client_id] = body
        self.log.append(("register", body["redirect_uris"]))
        return JSONResponse({**body, "client_id": client_id}, status_code=201)

    async def authorize(self, request):
        q = dict(request.query_params)
        self.log.append(("authorize", q["client_id"]))
        redirect = q["redirect_uri"]
        if self.deny:
            return RedirectResponse(f"{redirect}?error=access_denied&state={q['state']}")
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = (q["code_challenge"], redirect)
        return RedirectResponse(f"{redirect}?code={code}&state={q['state']}")

    async def token(self, request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        self.log.append(("token", form["grant_type"]))
        if form["grant_type"] == "authorization_code":
            challenge, redirect = self.codes.pop(form["code"], (None, None))
            verifier = form.get("code_verifier", "")
            expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge is None or challenge != expected or form.get("redirect_uri") != redirect:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._issue())
        if form["grant_type"] == "refresh_token":
            if self.revoke_refresh or form["refresh_token"] not in self.valid_refresh:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            return JSONResponse(self._issue())                     # rotates the refresh token
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def build_fake(fake):
    mcp = MCPServer("fake-tsenta")

    @mcp.tool(name="get-application-balance")
    def balance() -> str:
        return json.dumps({"tool": "get-application-balance", "result": {"total": 42, "monthly": {"remaining": 42, "limit": 100, "cycleEnd": "2026-12-18"}}})

    mcp_app = mcp.streamable_http_app()
    oauth_app = Starlette(routes=[
        Route("/.well-known/oauth-protected-resource/mcp", fake.prm), Route("/.well-known/oauth-protected-resource", fake.prm),
        Route("/.well-known/oauth-authorization-server", fake.metadata), Route("/oauth/register", fake.register, methods=["POST"]),
        Route("/oauth/authorize", fake.authorize), Route("/oauth/token", fake.token, methods=["POST"]),
    ])

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            return await mcp_app(scope, receive, send)
        path = scope["path"]
        if path.startswith("/oauth") or path.startswith("/.well-known"):
            return await oauth_app(scope, receive, send)
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        if auth.removeprefix("Bearer ") not in fake.valid_access:
            await send({"type": "http.response.start", "status": 401, "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", f'Bearer resource_metadata="{fake.base}/.well-known/oauth-protected-resource/mcp"'.encode())]})
            await send({"type": "http.response.body", "body": b'{"error":"invalid_token"}'})
            return
        return await mcp_app(scope, receive, send)

    return app


@pytest.fixture
def tsenta_server(tmp_path, monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    fake = FakeTsenta(f"http://127.0.0.1:{port}")
    server = uvicorn.Server(uvicorn.Config(build_fake(fake), host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started
    monkeypatch.setattr(cfg, "TSENTA_MCP_URL", f"{fake.base}/mcp")
    monkeypatch.setattr(cfg, "TSENTA_OAUTH_FILE", str(tmp_path / "instance" / "tsenta_oauth.json"))

    def fake_browser(url):                                          # the "user" approves in their browser
        threading.Thread(target=lambda: httpx2.get(url, follow_redirects=True, timeout=10), daemon=True).start()
        return True
    monkeypatch.setattr(login_tool.webbrowser, "open", fake_browser)
    yield fake
    server.should_exit = True
    thread.join(timeout=10)


def run_main(*args):
    return login_tool.main(list(args))


def test_the_whole_cycle_login_restart_and_silent_refresh(tsenta_server, capsys):
    fake = tsenta_server
    assert run_main("--check") == 1 and "tsenta_login.py" in capsys.readouterr().out          # nothing stored yet

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "Signed in to Tsenta" in out and "Applications left: 42" in out and "refresh token: yes" in out
    assert "access-" not in out and "refresh-" not in out and "code-" not in out               # no token ever printed
    storage = oauth.FileTokenStorage(cfg.TSENTA_OAUTH_FILE)
    assert storage.has_login() and storage.has_refresh_token() and storage.oauth_metadata()["token_endpoint"].endswith("/oauth/token")
    assert oct(__import__("os").stat(cfg.TSENTA_OAUTH_FILE).st_mode & 0o777) == "0o600"
    assert [e[0] for e in fake.log] == ["register", "authorize", "token"] and fake.log[2] == ("token", "authorization_code")   # PKCE verified by the fake

    time.sleep(1.5)                                                                             # the access token (1s) is now expired
    assert run_main("--check") == 0                                                             # a brand-new provider: refreshes, no browser
    assert "OK - applications left: 42" in capsys.readouterr().out
    assert fake.log[-1] == ("token", "refresh_token") and len([e for e in fake.log if e[0] == "authorize"]) == 1
    assert storage.has_refresh_token()

    # and the real client works through the same stored login, unattended
    time.sleep(1.5)
    with tm.TsentaClient(url=cfg.TSENTA_MCP_URL, oauth_file=cfg.TSENTA_OAUTH_FILE) as client:
        assert client.balance().total == 42
    assert len([e for e in fake.log if e[0] == "authorize"]) == 1                              # still only the one browser approval


def test_signing_in_again_needs_force(tsenta_server, capsys):
    assert run_main() == 0
    capsys.readouterr()
    assert run_main() == 0 and "Already signed in" in capsys.readouterr().out
    registrations = len([e for e in tsenta_server.log if e[0] == "register"])
    assert run_main("--force") == 0 and len([e for e in tsenta_server.log if e[0] == "register"]) == registrations + 1


def test_refusing_the_approval_leaves_nothing_stored(tsenta_server, capsys):
    tsenta_server.deny = True
    assert run_main() == 1
    assert "did not complete" in capsys.readouterr().out
    assert not oauth.FileTokenStorage(cfg.TSENTA_OAUTH_FILE).has_login()


def test_a_revoked_refresh_token_is_reported_not_hung_on(tsenta_server, capsys):
    assert run_main() == 0
    capsys.readouterr()
    time.sleep(1.5)
    tsenta_server.revoke_refresh = True
    start = time.time()
    assert run_main("--check") == 1
    assert time.time() - start < 20 and "tsenta_login.py" in capsys.readouterr().out
    assert [e for e in tsenta_server.log if e[0] == "authorize"] == [("authorize", "client-1")]      # never tried a second browser login


# ------------------------------------------------------------------ the dispatcher: browser vs MCP

@pytest.fixture
def paths(monkeypatch):
    calls = {"browser": [], "mcp": []}
    monkeypatch.setattr(tsenta, "apply_via_browser", lambda url: calls["browser"].append(url) or (True, "browser"))
    monkeypatch.setattr(tsenta, "resolve_apply_link", lambda url: url.replace("zapply.jobs/l/d/x", "real.example/job/1"))
    monkeypatch.setattr(tm, "apply_one_url", lambda url, **kw: calls["mcp"].append(url) or (True, "mcp"))
    return calls


def test_dry_run_keeps_every_apply_on_the_browser_even_when_signed_in(paths, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", True)
    monkeypatch.setattr(tm, "is_configured", lambda *a, **k: True)
    assert tsenta.apply_via_tsenta("https://a.example/1") == (True, "browser")
    assert paths == {"browser": ["https://a.example/1"], "mcp": []}


def test_no_mcp_sign_in_means_the_browser_even_after_go_live(paths, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    monkeypatch.setattr(tm, "is_configured", lambda *a, **k: False)
    assert tsenta.apply_via_tsenta("https://a.example/1") == (True, "browser")
    assert paths["mcp"] == []


def test_after_go_live_applies_go_through_the_mcp_with_the_resolved_link(paths, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    monkeypatch.setattr(tm, "is_configured", lambda *a, **k: True)
    assert tsenta.apply_via_tsenta("https://zapply.jobs/l/d/x") == (True, "mcp")
    assert paths == {"browser": [], "mcp": ["https://real.example/job/1"]}


def test_mcp_problems_come_back_as_a_failed_apply_with_the_reason(paths, monkeypatch):
    monkeypatch.setattr(cfg, "DRY_RUN", False)
    monkeypatch.setattr(tm, "is_configured", lambda *a, **k: True)

    def needs_login(url, **kw):
        raise oauth.NeedsLogin("expired")

    monkeypatch.setattr(tm, "apply_one_url", needs_login)
    ok, message = tsenta.apply_via_tsenta("https://a.example/1")
    assert not ok and "tsenta_login.py" in message

    def down(url, **kw):
        raise tm.TsentaError("connecting failed")

    monkeypatch.setattr(tm, "apply_one_url", down)
    ok, message = tsenta.apply_via_tsenta("https://a.example/1")
    assert not ok and "Could not reach Tsenta's MCP" in message
