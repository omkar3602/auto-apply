"""Token storage, and the SDK's OAuth provider driven against a mock Tsenta server."""
import asyncio
import json
import os
import stat
import time

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from automation import tsenta_oauth as oauth

MCP_URL = "https://api.autojobs.me/api/v1/mcp"
METADATA = {
    "issuer": "https://api.autojobs.me", "authorization_endpoint": "https://api.autojobs.me/oauth/authorize",
    "token_endpoint": "https://api.autojobs.me/oauth/token", "registration_endpoint": "https://api.autojobs.me/oauth/register",
    "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
}
CLIENT = OAuthClientInformationFull(client_id="client-123", redirect_uris=["http://127.0.0.1:9999/callback"],
                                    token_endpoint_auth_method="none", grant_types=["authorization_code", "refresh_token"],
                                    response_types=["code"], client_name="test")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def storage(tmp_path):
    return oauth.FileTokenStorage(tmp_path / "instance" / "tsenta_oauth.json")


def logged_in(storage, *, expires_in=3600, expired=False, refresh="refresh-1", access="access-1", metadata=True):
    run(storage.set_client_info(CLIENT))
    run(storage.set_tokens(OAuthToken(access_token=access, token_type="Bearer", expires_in=expires_in, refresh_token=refresh)))
    if expired:
        storage._update(expires_at=time.time() - 10)
    if metadata:
        storage.set_oauth_metadata(METADATA)


# ------------------------------------------------------------------ storage

def test_storage_round_trips_tokens_and_client(storage):
    logged_in(storage)
    tokens = run(storage.get_tokens())
    assert tokens.access_token == "access-1" and tokens.refresh_token == "refresh-1"
    assert run(storage.get_client_info()).client_id == "client-123"
    assert storage.expires_at() > time.time() + 3000
    assert storage.has_login() and storage.has_refresh_token()
    assert storage.oauth_metadata()["token_endpoint"].endswith("/oauth/token")


def test_storage_file_is_private_and_atomic(storage):
    logged_in(storage)
    assert stat.S_IMODE(os.stat(storage.path).st_mode) == 0o600
    assert [p.name for p in storage.path.parent.iterdir()] == ["tsenta_oauth.json"]       # no temp file left behind
    os.chmod(storage.path, 0o644)                                                         # someone loosens it: tightened on read
    storage._read()
    assert stat.S_IMODE(os.stat(storage.path).st_mode) == 0o600


def test_storage_tolerates_missing_and_corrupt_files(storage):
    assert not storage.has_login() and run(storage.get_tokens()) is None and run(storage.get_client_info()) is None
    storage.path.parent.mkdir(parents=True, exist_ok=True)
    storage.path.write_text("{not json")
    assert not storage.has_login() and run(storage.get_tokens()) is None


def test_has_login_needs_both_a_registration_and_a_token(storage):
    run(storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert not storage.has_login()                       # no client registration yet
    run(storage.set_client_info(CLIENT))
    assert storage.has_login() and not storage.has_refresh_token()


def test_clear_removes_everything(storage):
    logged_in(storage)
    storage.clear()
    assert not storage.path.exists() and not storage.has_login()
    storage.clear()                                      # clearing twice is fine


def test_token_without_expiry_has_no_absolute_expiry(storage):
    run(storage.set_tokens(OAuthToken(access_token="a", token_type="Bearer")))
    assert storage.expires_at() is None


# ------------------------------------------------------------------ the provider against a mock server

class MockTsenta:
    """Answers the MCP endpoint and the OAuth endpoints, and records what was asked."""

    def __init__(self, *, valid_access="access-2", refresh_ok=True, rotated_refresh="refresh-2"):
        self.valid_access, self.refresh_ok, self.rotated_refresh = valid_access, refresh_ok, rotated_refresh
        self.calls = []
        self.refresh_bodies = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.calls.append((request.method, url, request.headers.get("authorization")))
        if url.endswith("/oauth/token"):
            body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            self.refresh_bodies.append(body)
            if not self.refresh_ok:
                return httpx2.Response(400, json={"error": "invalid_grant"})
            return httpx2.Response(200, json={"access_token": self.valid_access, "token_type": "Bearer", "expires_in": 1800,
                                              "refresh_token": self.rotated_refresh})
        if ".well-known/oauth-protected-resource" in url:
            return httpx2.Response(200, json={"resource": MCP_URL, "authorization_servers": ["https://api.autojobs.me"]})
        if ".well-known/oauth-authorization-server" in url:
            return httpx2.Response(200, json=METADATA)
        if url == MCP_URL:
            if request.headers.get("authorization") == f"Bearer {self.valid_access}":
                return httpx2.Response(200, json={"ok": True})
            return httpx2.Response(401, headers={"www-authenticate": 'Bearer resource_metadata="https://api.autojobs.me/.well-known/oauth-protected-resource/api/v1/mcp"'},
                                   json={"error": "invalid_token"})
        return httpx2.Response(404)


def call_mcp(provider, server):
    async def go():
        async with httpx2.AsyncClient(auth=provider, transport=httpx2.MockTransport(server)) as http:
            return await http.get(MCP_URL)
    return run(go())


def test_an_expired_token_is_refreshed_silently_at_the_right_endpoint_and_the_rotation_is_saved(storage):
    logged_in(storage, expired=True, access="stale")
    server = MockTsenta()
    response = call_mcp(oauth.background_provider(MCP_URL, storage), server)
    assert response.status_code == 200
    # refreshed against Tsenta's real token endpoint (restored from storage), not the SDK's default /token
    assert [c[1] for c in server.calls if "oauth/token" in c[1] or c[1].endswith("/token")] == ["https://api.autojobs.me/oauth/token"]
    assert server.refresh_bodies[0]["grant_type"] == "refresh_token" and server.refresh_bodies[0]["refresh_token"] == "refresh-1"
    assert server.refresh_bodies[0]["client_id"] == "client-123"
    # the request used the NEW token; the stale one was never sent
    assert [c[2] for c in server.calls if c[1] == MCP_URL] == ["Bearer access-2"]
    # and both the new access token and the rotated refresh token were persisted
    saved = run(storage.get_tokens())
    assert saved.access_token == "access-2" and saved.refresh_token == "refresh-2"
    assert storage.expires_at() > time.time() + 1500


def test_a_valid_token_is_used_as_is_with_no_refresh(storage):
    logged_in(storage, access="access-2")
    server = MockTsenta()
    assert call_mcp(oauth.background_provider(MCP_URL, storage), server).status_code == 200
    assert not [c for c in server.calls if "oauth" in c[1]]


def test_refresh_without_stored_metadata_still_finds_the_token_endpoint(storage):
    logged_in(storage, expired=True, metadata=False)
    server = MockTsenta()
    provider = oauth.background_provider(MCP_URL, storage)
    provider.metadata_transport = httpx2.MockTransport(server)
    assert call_mcp(provider, server).status_code == 200
    assert any(c[1].endswith("/.well-known/oauth-authorization-server") for c in server.calls)      # fetched it instead
    assert any(c[1] == "https://api.autojobs.me/oauth/token" for c in server.calls)


def test_a_dead_refresh_token_raises_needs_login_instead_of_opening_a_browser(storage):
    logged_in(storage, expired=True)
    server = MockTsenta(refresh_ok=False)
    with pytest.raises(BaseException) as info:
        call_mcp(oauth.background_provider(MCP_URL, storage), server)
    found = oauth.find_needs_login(info.value)
    assert found is not None and "tools/tsenta_login.py" in str(found)


def test_a_rejected_token_that_looked_valid_also_raises_needs_login(storage):
    logged_in(storage, access="revoked-token")             # not expired on paper, but the server says no
    server = MockTsenta(valid_access="something-else")
    with pytest.raises(BaseException) as info:
        call_mcp(oauth.background_provider(MCP_URL, storage), server)
    assert oauth.find_needs_login(info.value) is not None
    assert not [c for c in server.calls if c[1].endswith("/oauth/register")]       # never re-registers in the background


def test_find_needs_login_digs_through_wrappers():
    inner = oauth.NeedsLogin("x")
    wrapped = ExceptionGroup("tasks", [RuntimeError("other"), ExceptionGroup("inner", [inner])])
    assert oauth.find_needs_login(wrapped) is inner
    chained = RuntimeError("outer")
    chained.__cause__ = inner
    assert oauth.find_needs_login(chained) is inner
    assert oauth.find_needs_login(RuntimeError("unrelated")) is None


def test_client_metadata_is_a_public_client_with_refresh_tokens():
    meta = oauth.client_metadata("http://127.0.0.1:5555/callback")
    assert meta.token_endpoint_auth_method == "none" and "refresh_token" in meta.grant_types and "authorization_code" in meta.grant_types
    assert [str(u) for u in meta.redirect_uris] == ["http://127.0.0.1:5555/callback"]


def test_no_secret_is_ever_written_outside_the_oauth_file(storage, tmp_path):
    logged_in(storage, access="SECRET-ACCESS", refresh="SECRET-REFRESH")
    leaked = [p for p in tmp_path.rglob("*") if p.is_file() and p != storage.path and b"SECRET" in p.read_bytes()]
    assert leaked == []
    assert json.loads(storage.path.read_text())["tokens"]["access_token"] == "SECRET-ACCESS"
