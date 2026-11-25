"""Signing in to Tsenta's MCP server without a browser, after one browser login.

Tsenta's MCP endpoint takes OAuth 2.1 bearer tokens (authorization code + PKCE,
refresh tokens, public client, dynamic registration - checked 2026-11-25), and
offers no static API key. So: run tools/tsenta_login.py ONCE; it stores the
tokens in instance/tsenta_oauth.json (git-ignored, mode 600). From then on this
module refreshes access tokens by itself.

Two things the MCP SDK does that would break an unattended scheduler, and how
this module handles them:

1. After a restart the SDK doesn't know when a stored access token expires, so it
   sends it, gets a 401, and jumps to an INTERACTIVE browser login without ever
   trying the refresh token. Fix: persist an absolute expiry and hand it back to
   the SDK, so an expired token is refreshed first.
2. The SDK's refresh request needs the server's token endpoint, which it only
   learns by discovery during a 401. Fix: persist the server's OAuth metadata at
   login and hand it back on start.

Anything that would still need a browser (refresh token expired or revoked)
raises NeedsLogin instead of waiting for one, so the scheduler can say so.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx2
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthMetadata, OAuthToken

LOGIN_COMMAND = ".venv/bin/python tools/tsenta_login.py"
EXPIRY_SKEW_S = 60          # treat a token as expired a minute early
CLIENT_NAME = "AutoApply (personal scheduler)"


class NeedsLogin(Exception):
    """Tsenta needs a fresh browser login (nothing stored, or the refresh token is gone)."""

    def __init__(self, why="Tsenta sign-in is missing or expired"):
        super().__init__(f"{why} - run: {LOGIN_COMMAND}")


class FileTokenStorage:
    """The SDK's TokenStorage, backed by one JSON file that only you can read.

    Writes are atomic (temp file + rename, created mode 600), so a crash mid-write
    can't leave half a token, and a rotated refresh token is saved before it's used.
    Besides the SDK's tokens and client registration it keeps the absolute expiry
    and the server's OAuth metadata (see the module docstring)."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    # ----- plain file access
    def _read(self):
        try:
            with open(self.path) as fh:
                data = json.load(fh)
        except (FileNotFoundError, ValueError):
            return {}
        try:
            if os.stat(self.path).st_mode & 0o077:
                os.chmod(self.path, 0o600)
        except OSError:
            pass
        return data if isinstance(data, dict) else {}

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".tsenta_oauth.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(data, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _update(self, **fields):
        with self._lock:
            data = self._read()
            data.update(fields)
            self._write(data)

    # ----- the SDK's protocol
    async def get_tokens(self) -> Optional[OAuthToken]:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        expires_at = time.time() + tokens.expires_in if tokens.expires_in else None
        self._update(tokens=tokens.model_dump(mode="json", exclude_none=True), expires_at=expires_at, saved_at=time.time())

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._update(client_info=client_info.model_dump(mode="json", exclude_none=True))

    # ----- extras
    def expires_at(self):
        return self._read().get("expires_at")

    def oauth_metadata(self):
        return self._read().get("oauth_metadata")

    def set_oauth_metadata(self, metadata: dict) -> None:
        self._update(oauth_metadata=metadata)

    def has_login(self) -> bool:
        """Is there a stored registration plus a token that can be used or refreshed?"""
        data = self._read()
        tokens = data.get("tokens") or {}
        return bool(data.get("client_info") and (tokens.get("refresh_token") or tokens.get("access_token")))

    def has_refresh_token(self) -> bool:
        return bool((self._read().get("tokens") or {}).get("refresh_token"))

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


class TsentaOAuthProvider(OAuthClientProvider):
    """The SDK provider plus the two fixes in the module docstring."""

    metadata_transport = None        # tests inject a mock transport here; None = real network

    async def _initialize(self) -> None:
        await super()._initialize()
        storage = self.context.storage
        expires_at = storage.expires_at() if hasattr(storage, "expires_at") else None
        if expires_at is not None and self.context.current_tokens is not None:
            self.context.token_expiry_time = float(expires_at) - EXPIRY_SKEW_S
        if self.context.oauth_metadata is None:
            stored = storage.oauth_metadata() if hasattr(storage, "oauth_metadata") else None
            if stored:
                try:
                    self.context.oauth_metadata = OAuthMetadata.model_validate(stored)
                except ValueError:
                    stored = None
            if self.context.oauth_metadata is None:
                await self._fetch_metadata()

    async def _fetch_metadata(self) -> None:
        origin = urlsplit(self.context.server_url)
        url = f"{origin.scheme}://{origin.netloc}/.well-known/oauth-authorization-server"
        try:
            async with httpx2.AsyncClient(timeout=20, transport=self.metadata_transport) as http:
                response = await http.get(url)
            if response.status_code == 200:
                self.context.oauth_metadata = OAuthMetadata.model_validate(response.json())
        except Exception:  # noqa: BLE001 - best effort; the refresh then falls back to the SDK default
            pass


def client_metadata(redirect_uri: str) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


def background_provider(server_url: str, storage: FileTokenStorage) -> TsentaOAuthProvider:
    """For the scheduler: refreshes silently; if it would need a browser it raises NeedsLogin.

    `provider.login_flag["needed"]` is also set, because the SDK's transport can reduce the
    exception to a generic "server returned an error" by the time the caller sees it."""
    flag = {"needed": False}

    async def no_browser(_url: str) -> None:
        flag["needed"] = True
        raise NeedsLogin("Tsenta sign-in expired or was revoked")

    async def no_callback():
        flag["needed"] = True
        raise NeedsLogin("Tsenta sign-in expired or was revoked")

    provider = TsentaOAuthProvider(
        server_url=server_url,
        client_metadata=client_metadata("http://127.0.0.1/callback"),   # unused: already registered
        storage=storage,
        redirect_handler=no_browser,
        callback_handler=no_callback,
    )
    provider.login_flag = flag
    return provider


def find_needs_login(exc: BaseException) -> Optional[NeedsLogin]:
    """The SDK runs requests inside task groups, so a NeedsLogin can arrive wrapped
    in an ExceptionGroup or as a __cause__; dig it out."""
    seen = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, NeedsLogin):
            return current
        stack.append(current.__cause__)
        stack.append(current.__context__)
        stack.extend(getattr(current, "exceptions", ()) or ())
    return None
