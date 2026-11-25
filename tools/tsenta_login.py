#!/usr/bin/env python
"""One-time sign-in to Tsenta's MCP server, so the scheduler can use it unattended.

    .venv/bin/python tools/tsenta_login.py            sign in (opens your browser, you approve once)
    .venv/bin/python tools/tsenta_login.py --check    test the stored sign-in; read-only, no browser
    .venv/bin/python tools/tsenta_login.py --force    sign in again, replacing what's stored

What it does: registers this app with Tsenta as an OAuth client, opens Tsenta's
approval page in your browser, catches the answer on http://127.0.0.1:<port>/callback,
and saves the resulting tokens to instance/tsenta_oauth.json (git-ignored, mode 600).
It never prints a token. After that, tokens refresh by themselves; you only come
back here if the refresh token is revoked or expires (the scheduler will tell you).

Whether Tsenta issues a refresh token, and for how long it lives, isn't documented -
this script reports what it actually got.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx2  # noqa: E402
from mcp.client.session import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.auth import AuthorizationCodeResult  # noqa: E402

import config as cfg  # noqa: E402
from automation import tsenta_mcp, tsenta_oauth as oauth  # noqa: E402

LOGIN_TIMEOUT_S = 300
_PAGE = (b"<!doctype html><meta charset=utf-8><title>Tsenta sign-in</title>"
         b"<body style='font:16px system-ui;margin:3rem'><h2>Signed in</h2><p>You can close this tab and go back to the terminal.</p>")
_ERROR_PAGE = b"<!doctype html><meta charset=utf-8><title>Tsenta sign-in</title><body style='font:16px system-ui;margin:3rem'><h2>Sign-in failed</h2><p>Go back to the terminal.</p>"


class CallbackServer:
    """Catches the OAuth redirect on a free loopback port."""

    def __init__(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.result = None
        self._done = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):                                   # noqa: N802 - http.server's name
                parts = urlsplit(self.path)
                if parts.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return
                query = {k: v[0] for k, v in parse_qs(parts.query).items()}
                outer.result = query
                ok = "code" in query and "error" not in query
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_PAGE if ok else _ERROR_PAGE)
                outer._done.set()

            def log_message(self, *args):                      # keep the terminal clean (the URL has the code in it)
                pass

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def redirect_uri(self):
        return f"http://127.0.0.1:{self.port}/callback"

    def wait(self, timeout):
        if not self._done.wait(timeout):
            raise TimeoutError("no answer from the browser in time")
        return self.result

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _describe_tokens(storage):
    expires_at = storage.expires_at()
    left = f"{max(0, int((expires_at - time.time()) // 60))} min" if expires_at else "unknown"
    return left, storage.has_refresh_token()


async def _balance_through(provider, url):
    async with httpx2.AsyncClient(auth=provider, timeout=httpx2.Timeout(60)) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("get-application-balance", {})
    return tsenta_mcp.parse_balance(tsenta_mcp._payload("get-application-balance", result))


async def login(force):
    storage = oauth.FileTokenStorage(cfg.TSENTA_OAUTH_FILE)
    if storage.has_login() and not force:
        print("Already signed in. Use --check to test it, or --force to sign in again.")
        return 0
    storage.clear()                                            # a fresh registration, so the new callback port matches
    server = CallbackServer()
    loop = asyncio.get_running_loop()

    async def open_browser(auth_url):
        print("\nOpening your browser to approve the sign-in. If it doesn't open, paste this address into it:\n")
        print(f"  {auth_url}\n")
        webbrowser.open(auth_url)

    async def wait_for_answer():
        query = await loop.run_in_executor(None, server.wait, LOGIN_TIMEOUT_S)
        if "error" in query or "code" not in query:
            raise oauth.NeedsLogin(f"Tsenta refused the sign-in ({query.get('error', 'no code returned')})")
        return AuthorizationCodeResult(code=query["code"], state=query.get("state"), iss=query.get("iss"))

    provider = oauth.TsentaOAuthProvider(
        server_url=cfg.TSENTA_MCP_URL, client_metadata=oauth.client_metadata(server.redirect_uri), storage=storage,
        redirect_handler=open_browser, callback_handler=wait_for_answer,
    )
    try:
        balance = await _balance_through(provider, cfg.TSENTA_MCP_URL)
        if provider.context.oauth_metadata is not None:
            storage.set_oauth_metadata(provider.context.oauth_metadata.model_dump(mode="json", exclude_none=True))
    except BaseException as exc:                              # noqa: BLE001
        print(f"\nSign-in did not complete: {exc}")
        storage.clear()
        return 1
    finally:
        server.close()
    left, has_refresh = _describe_tokens(storage)
    print("Signed in to Tsenta. Applications left:", balance.total, f"(monthly allowance resets {balance.cycle_end})")
    print(f"  access token valid for about {left}; refresh token: {'yes - it will renew by itself' if has_refresh else 'NO - tokens will lapse and you must sign in again'}")
    print(f"  stored in {os.path.relpath(cfg.TSENTA_OAUTH_FILE)} (not in git, owner-only)")
    if not has_refresh:
        print("\nWarning: Tsenta did not issue a refresh token, so the scheduler can't stay signed in unattended.")
    return 0


async def check():
    storage = oauth.FileTokenStorage(cfg.TSENTA_OAUTH_FILE)
    if not storage.has_login():
        print(f"Not signed in. Run: {oauth.LOGIN_COMMAND}")
        return 1
    left, has_refresh = _describe_tokens(storage)
    print(f"Stored sign-in found (access token ~{left} left, refresh token: {'yes' if has_refresh else 'no'}). Testing it without a browser...")
    try:
        balance = await _balance_through(oauth.background_provider(cfg.TSENTA_MCP_URL, storage), cfg.TSENTA_MCP_URL)
    except BaseException as exc:                              # noqa: BLE001
        needs = oauth.find_needs_login(exc)
        print(str(needs) if needs else f"Failed: {exc}")
        return 1
    left, has_refresh = _describe_tokens(storage)
    print(f"OK - applications left: {balance.total}; access token now valid for ~{left}; refresh token: {'yes' if has_refresh else 'no'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="One-time sign-in to Tsenta's MCP server.")
    parser.add_argument("--check", action="store_true", help="test the stored sign-in (no browser)")
    parser.add_argument("--force", action="store_true", help="sign in again even if already signed in")
    args = parser.parse_args(argv)
    return asyncio.run(check() if args.check else login(args.force))


if __name__ == "__main__":
    sys.exit(main())
