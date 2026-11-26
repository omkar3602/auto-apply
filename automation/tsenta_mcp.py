"""Talks to Tsenta's MCP server (https://api.autojobs.me/api/v1/mcp) instead of
driving its website: search the job feed, read descriptions, apply, and check
what really happened to each application.

Synchronous on the outside (the app is Flask + threads), asynchronous inside: each
`with TsentaClient.from_env() as tsenta:` block runs one MCP session on a private
event-loop thread. Everything that can cost money goes through `apply_*`, which:

- refuses to run unless the client was created with allow_apply=True (the
  scheduler passes `not DRY_RUN`), and
- is never retried automatically - a retry after a timeout could submit twice, so
  an unclear result comes back as category "unknown" and is settled afterwards
  with `wait_for_outcomes` (which reads Tsenta's own record).

Credentials, in order: the TSENTA_MCP_TOKEN environment variable (a raw bearer
token, e.g. for a quick test; it expires and is never refreshed), else the stored
browser login from tools/tsenta_login.py (refreshed automatically, see
automation/tsenta_oauth.py). With neither, `is_configured()` is False and callers
fall back to the browser flow.

What the real responses look like was checked against a live account on
2026-11-25 for: get-job-recommendations, get-jobs-by-ids, list-applications,
get-application-balance. The shape of apply-to-job's reply was NOT checked (it
can't be without applying), so parse_apply_response is deliberately tolerant and
keeps the raw reply for the log.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import config as cfg
from automation import tsenta_oauth as oauth
from automation.filters import EXCLUDE_QUERY, SEARCH_QUERY

log = logging.getLogger(__name__)

NeedsLogin = oauth.NeedsLogin
DEFAULT_LOCATION_SETS = (["country:US"], ["state:NY"])
# The scheduler's age limit is 24h (filters.MAX_AGE), so ask Tsenta for exactly that. A 7-day window also
# made the New York search fail with TEMPORARILY_UNAVAILABLE on 2026-11-25, and its 400-result page limit
# was being spent on postings the age rule then threw away.
FEED_DATE_WINDOW = "24h"
FEED_ATTEMPTS = 3                 # tries per feed page before giving up on that search
FEED_RETRY_WAIT_S = (2, 6)        # pauses before the 2nd and 3rd try
APPLY_CHUNK = 10                  # jobs per apply-to-job call: bounds the damage if a call fails midway
DESCRIPTION_BUDGET = 48_000       # get-jobs-by-ids shares this many characters across a call
_SESSION_LOCK = threading.Lock()  # one Tsenta session at a time in this process (token refresh isn't re-entrant)


class TsentaError(Exception):
    pass


class TsentaToolError(TsentaError):
    """The server answered, but the tool itself reported an error."""


class NotConfigured(TsentaError):
    pass


class ApplyBlocked(TsentaError):
    """An apply was attempted while applying is switched off (DRY_RUN)."""


# ----------------------------------------------------------------- configuration

def static_token():
    return (os.environ.get("TSENTA_MCP_TOKEN") or "").strip() or None


def is_configured(oauth_file=None):
    """Can we talk to Tsenta's MCP at all right now (a token in the environment, or a stored login)?"""
    return bool(static_token()) or oauth.FileTokenStorage(oauth_file or cfg.TSENTA_OAUTH_FILE).has_login()


# ----------------------------------------------------------------- parsed results

@dataclass
class Balance:
    total: int
    monthly_remaining: int
    monthly_limit: int
    cycle_end: Optional[str]
    admin_bypass: bool
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def has_room(self):
        return self.admin_bypass or self.total > 0


@dataclass
class ApplyResult:
    ref: str                              # the job id (feed jobs) or URL (direct)
    success: bool
    category: str                         # applied | already_applied | closed | unsupported | out_of_balance | profile_missing | error | unknown
    message: str = ""
    application_id: Optional[str] = None
    status: Optional[str] = None
    raw: Any = field(default=None, repr=False)

    @property
    def stop_run(self):
        """Nothing else will work this run: no credits, or the profile is incomplete."""
        return self.category in ("out_of_balance", "profile_missing")


@dataclass
class Outcome:
    """What Tsenta itself says happened to one application (from list-applications)."""

    application_id: Optional[str]
    status: str                           # COMPLETED | FAILED | SKIPPED | PENDING_* | RUNNING | ...
    kind: str                             # applied | failed | needs_you | pending
    message: str = ""
    label: str = ""
    error_code: Optional[str] = None
    can_reapply: bool = False
    will_retry: bool = False              # Tsenta says it retries this failure by itself
    url: Optional[str] = None
    company: Optional[str] = None
    title: Optional[str] = None
    missing: list = field(default_factory=list)
    raw: Any = field(default=None, repr=False)


_APPLY_ERRORS = (
    ("out_of_balance", re.compile(r"out of applications|none left", re.I)),
    ("profile_missing", re.compile(r"profile is missing", re.I)),
    ("already_applied", re.compile(r"already applied", re.I)),
    ("closed", re.compile(r"closed|expired|no longer (?:available|accepting)", re.I)),
    ("unsupported", re.compile(r"don'?t support|do not support|not support|unsupported", re.I)),
)


def classify_apply_error(message):
    for category, pattern in _APPLY_ERRORS:
        if pattern.search(message or ""):
            return category
    return "error"


def parse_balance(payload):
    monthly = payload.get("monthly") or {}
    return Balance(
        total=int(payload.get("total") or 0),
        monthly_remaining=int(monthly.get("remaining") or 0),
        monthly_limit=int(monthly.get("limit") or 0),
        cycle_end=monthly.get("cycleEnd"),
        admin_bypass=bool(payload.get("isAdminBypass")),
        raw=payload,
    )


def _first_text(item, *keys):
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = " - ".join(str(value[k]) for k in ("title", "description", "message") if value.get(k))
            if nested:
                return nested
    return ""


def parse_apply_response(payload, refs):
    """One ApplyResult per requested ref, in order. Tolerant of the reply's exact shape."""
    payload = payload if isinstance(payload, dict) else {}
    items = next((payload[k] for k in ("results", "jobs", "applications", "items") if isinstance(payload.get(k), list)), None)
    if items is None:
        items = [payload] if ("success" in payload or "error" in payload) and len(refs) == 1 else []
    top_error = _first_text(payload, "error", "message", "reason")
    by_key = {}
    for item in items:
        if isinstance(item, dict):
            for key in ("jobId", "id", "url", "ref"):
                if item.get(key):
                    by_key.setdefault(str(item[key]), item)
    results = []
    for index, ref in enumerate(refs):
        item = by_key.get(str(ref)) or (items[index] if index < len(items) and isinstance(items[index], dict) else None)
        if item is None:
            if top_error and not items:                     # the whole call was refused (e.g. out of applications)
                results.append(ApplyResult(ref, False, classify_apply_error(top_error), top_error, raw=payload))
            else:
                results.append(ApplyResult(ref, False, "unknown", "Tsenta returned no result for this job - check its application list", raw=payload))
            continue
        success = bool(item.get("success"))
        message = _first_text(item, "error", "message", "reason", "errorMessage")
        results.append(ApplyResult(
            ref, success, "applied" if success else classify_apply_error(message or top_error), message or top_error,
            application_id=item.get("applicationId") or item.get("application_id"), status=item.get("status"), raw=item,
        ))
    return results


def outcome_from_row(row):
    status = str(row.get("status") or "")
    message = _first_text(row, "statusMessage") or str(row.get("statusLabel") or "")
    missing = [m.get("label") for m in (row.get("missingRequired") or []) if isinstance(m, dict) and m.get("label")]
    if status == "COMPLETED":
        kind = "applied"
    elif status in ("FAILED", "SKIPPED"):
        kind = "failed"
        if status == "SKIPPED" and not message:
            message = "Skipped by Tsenta"
    elif status.startswith("PENDING_"):
        kind = "needs_you"
        if missing:
            message = f"{message} (needs: {'; '.join(missing[:3])})" if message else f"needs: {'; '.join(missing[:3])}"
    else:
        kind = "pending"
    return Outcome(
        application_id=row.get("applicationId"), status=status, kind=kind, message=message, label=str(row.get("statusLabel") or ""),
        error_code=row.get("errorCode"), can_reapply=bool(row.get("canReapply")), will_retry="retried automatically" in message.lower(),
        url=row.get("url"), company=row.get("companyName"), title=row.get("jobTitle"), missing=missing, raw=row,
    )


def html_to_text(value):
    """Tsenta's descriptions are HTML; the rules read plain text."""
    if not value:
        return ""
    text = re.sub(r"(?i)<\s*(?:br|/p|/li|/div|/h[1-6]|/tr)\s*/?\s*>", "\n", value)
    text = re.sub(r"(?i)<\s*li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


# ----------------------------------------------------------------- the connection

def _payload(name, result):
    """The reply as a dict. Tsenta returns JSON text shaped {"tool": ..., "result": {...}}; that text is
    what's trusted, with the SDK's `structured_content` only as a fallback."""
    texts = [c.text for c in (result.content or []) if getattr(c, "text", None)]
    data = None
    for text in texts:
        try:
            parsed = json.loads(text)
        except ValueError:
            continue
        if isinstance(parsed, (dict, list)):
            data = parsed
            break
    if data is None:
        structured = result.structured_content
        data = structured if isinstance(structured, dict) and structured else ({"text": texts[0]} if texts else None)
    if result.is_error:
        detail = _first_text(data, "error", "message", "text") if isinstance(data, dict) else ""
        raise TsentaToolError(f"{name}: {detail or (texts[0] if texts else 'the tool reported an error')}")
    if isinstance(data, dict) and isinstance(data.get("result"), (dict, list)):
        data = data["result"]
    return data if data is not None else {}


def _is_unauthorized(exc):
    seen, stack = set(), [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        response = getattr(current, "response", None)
        if getattr(response, "status_code", None) == 401 or "401 Unauthorized" in str(current):
            return True
        stack.extend([current.__cause__, current.__context__, *(getattr(current, "exceptions", ()) or ())])
    return False


class _Session:
    """One MCP session, owned by a single task on a private event loop (the SDK's
    transport must be opened and closed by the same task)."""

    def __init__(self, url, auth, headers, http_timeout, read_timeout):
        self.url, self.auth, self.headers = url, auth, headers
        self.http_timeout, self.read_timeout = http_timeout, read_timeout
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="tsenta-mcp", daemon=True)
        self._queue = None
        self._task = None

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _submit(self, coro, timeout):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def open(self, timeout=60):
        self._thread.start()
        self._submit(self._start(), timeout)

    async def _start(self):
        self._queue = asyncio.Queue()
        ready = self.loop.create_future()
        self._task = self.loop.create_task(self._owner(ready))
        await ready

    async def _owner(self, ready):
        stack = AsyncExitStack()
        try:
            http = await stack.enter_async_context(httpx2.AsyncClient(auth=self.auth, headers=self.headers, timeout=httpx2.Timeout(self.http_timeout)))
            streams = await stack.enter_async_context(streamable_http_client(self.url, http_client=http))
            session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
            await session.initialize()
        except BaseException as exc:  # noqa: BLE001 - handed to the caller waiting on `ready`
            ready.set_exception(exc)
            await _quiet_close(stack)
            return
        ready.set_result(None)
        while True:
            item = await self._queue.get()
            if item is None:
                break
            name, args, future = item
            try:
                result = await session.call_tool(name, args, read_timeout_seconds=self.read_timeout)
                future.set_result(_payload(name, result))
            except BaseException as exc:  # noqa: BLE001 - the caller decides what to do
                future.set_exception(exc)
        await _quiet_close(stack)

    async def _call(self, name, args):
        future = self.loop.create_future()
        await self._queue.put((name, args, future))
        return await future

    def call(self, name, args, timeout):
        return self._submit(self._call(name, args), timeout)

    def close(self):
        try:
            if self._task is not None and not self._task.done():
                self._submit(self._stop(), 30)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            if self._thread.is_alive():
                self._thread.join(timeout=5)

    async def _stop(self):
        await self._queue.put(None)
        await self._task


async def _quiet_close(stack):
    try:
        await stack.aclose()
    except BaseException:  # noqa: BLE001 - closing a half-open session must not mask the real error
        pass


# ----------------------------------------------------------------- the client

class TsentaClient:
    def __init__(self, *, url=None, token=None, oauth_file=None, allow_apply=False, call_timeout=120, http_timeout=90):
        self.url = url or cfg.TSENTA_MCP_URL
        self.token = token
        self.storage = oauth.FileTokenStorage(oauth_file or cfg.TSENTA_OAUTH_FILE)
        self.allow_apply = allow_apply
        self.call_timeout, self.http_timeout = call_timeout, http_timeout
        self.last_feed_notes: list = []
        self._session: Optional[_Session] = None
        self._auth = None
        self._sleep = time.sleep

    @classmethod
    def from_env(cls, *, allow_apply=False, oauth_file=None, **kwargs):
        token = static_token()
        client = cls(token=token, oauth_file=oauth_file, allow_apply=allow_apply, **kwargs)
        if not token and not client.storage.has_login():
            raise NotConfigured(f"no Tsenta sign-in found - run: {oauth.LOGIN_COMMAND} (or set TSENTA_MCP_TOKEN)")
        return client

    # ----- session
    def __enter__(self):
        _SESSION_LOCK.acquire()
        try:
            self._connect()
        except BaseException:
            _SESSION_LOCK.release()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._disconnect()
        finally:
            _SESSION_LOCK.release()

    def _connect(self):
        if self.token:
            auth, headers = None, {"Authorization": f"Bearer {self.token}"}
        else:
            auth, headers = oauth.background_provider(self.url, self.storage), {}
        self._auth = auth
        session = _Session(self.url, auth, headers, self.http_timeout, self.call_timeout)
        try:
            session.open()
        except Exception as exc:
            session.close()
            self._raise_for(exc, "connecting")
        self._session = session

    def _disconnect(self):
        if self._session is not None:
            self._session.close()
            self._session = None

    def _login_was_needed(self):
        flag = getattr(self._auth, "login_flag", None)
        return bool(flag and flag.get("needed"))

    def _probe_unauthorized(self):
        """The SDK turns a 401 during connect into a bare "Server returned an error response", so ask
        the server directly whether the static token is the problem."""
        if not self.token:
            return False
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "auth-probe", "version": "0"}}}
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        try:
            with httpx2.Client(timeout=15) as http:
                return http.post(self.url, headers=headers, json=body).status_code == 401
        except Exception:  # noqa: BLE001
            return False

    def _raise_for(self, exc, what):
        needs = oauth.find_needs_login(exc)
        if needs:
            raise needs from None
        if self._login_was_needed():
            raise NeedsLogin("Tsenta sign-in expired or was revoked") from None
        if _is_unauthorized(exc) or (what == "connecting" and not isinstance(exc, TsentaError) and self._probe_unauthorized()):
            raise NeedsLogin("Tsenta rejected the token") from None
        if isinstance(exc, TsentaError):
            raise exc
        raise TsentaError(f"{what} failed: {exc}") from exc

    def _call(self, name, args, *, retry=True):
        """One tool call. Reads reconnect and retry once; anything that isn't a read passes retry=False."""
        if self._session is None:
            raise TsentaError("not connected - use `with TsentaClient.from_env() as tsenta:`")
        attempts = 2 if retry else 1
        for attempt in range(attempts):
            try:
                return self._session.call(name, args, self.call_timeout + 15)
            except TsentaToolError:
                raise
            except Exception as exc:  # noqa: BLE001
                if oauth.find_needs_login(exc) or _is_unauthorized(exc) or attempt + 1 >= attempts:
                    self._raise_for(exc, name)
                log.warning("[tsenta] %s failed (%s); reconnecting once", name, type(exc).__name__)
                self._disconnect()
                self._connect()

    # ----- reads
    def balance(self):
        return parse_balance(self._call("get-application-balance", {}))

    def recommendations(self, *, search, exclude_search=None, locations=None, date_posted="7d", limit=50, page=1, needs_sponsorship=True, **extra):
        """One page of the feed -> (jobs, has_more, payload)."""
        args = {"search": search, "datePosted": date_posted, "limit": limit, "page": page, "needsSponsorship": needs_sponsorship, **extra}
        if exclude_search:
            args["excludeSearch"] = exclude_search
        if locations:
            args["locations"] = list(locations)
        payload = self._call("get-job-recommendations", args)
        return list(payload.get("jobs") or []), bool(payload.get("hasMore")), payload

    def _feed_page(self, **kwargs):
        """One feed page, retried when Tsenta says it is temporarily unavailable or the call drops.
        Returns (jobs, has_more, payload); raises TsentaError once the attempts are used up, or at
        once for NO_PROFILE (retrying can't fix an unfinished profile). A sign-in problem always propagates."""
        last = None
        for attempt in range(FEED_ATTEMPTS):
            if attempt:
                self._sleep(FEED_RETRY_WAIT_S[min(attempt - 1, len(FEED_RETRY_WAIT_S) - 1)])
            try:
                jobs, more, payload = self.recommendations(**kwargs)
            except NeedsLogin:
                raise
            except TsentaError as exc:
                last = exc
                continue
            reason = payload.get("zeroResultReason")
            if not jobs and reason == "NO_PROFILE":
                raise TsentaError("Tsenta feed unavailable (NO_PROFILE): finish your Tsenta profile")
            if not jobs and reason == "TEMPORARILY_UNAVAILABLE":
                last = TsentaError("Tsenta feed unavailable (TEMPORARILY_UNAVAILABLE)")
                continue
            return jobs, more, payload
        raise last

    def feed(self, *, search=SEARCH_QUERY, exclude_search=EXCLUDE_QUERY, location_sets=DEFAULT_LOCATION_SETS, date_posted=FEED_DATE_WINDOW,
             limit=50, max_pages=20, needs_sponsorship=True):
        """Every page of the feed for each location set, de-duplicated by Tsenta job id.

        A page that keeps failing (after retries) ends only its own location set: what was already
        fetched is kept and a note is added to `last_feed_notes`. Only a feed that produced nothing at
        all because of failures raises. Notes about odd replies (zero-result reasons) end up there too."""
        self.last_feed_notes = []
        found = {}
        failure = None
        for locations in location_sets:
            for page in range(1, max_pages + 1):
                try:
                    jobs, more, payload = self._feed_page(
                        search=search, exclude_search=exclude_search, locations=locations, date_posted=date_posted,
                        limit=limit, page=page, needs_sponsorship=needs_sponsorship)
                except TsentaError as exc:
                    if "NO_PROFILE" in str(exc):
                        raise
                    failure = exc
                    self.last_feed_notes.append(f"{locations}: page {page} failed after {FEED_ATTEMPTS} tries ({exc}); kept the {len(found)} job(s) fetched so far")
                    break
                reason = payload.get("zeroResultReason")
                # PAGINATION_EXCEEDED just means "no page after the last one" once we
                # already have results; it isn't a problem worth reporting.
                if not jobs and reason and not (reason == "PAGINATION_EXCEEDED" and found):
                    self.last_feed_notes.append(f"{locations}: no results ({reason})")
                for job in jobs:
                    if job.get("id"):
                        found.setdefault(job["id"], job)
                if not more:
                    break
            else:
                self.last_feed_notes.append(f"{locations}: stopped at {max_pages} pages")
        if not found and failure is not None:
            raise failure
        return list(found.values())

    def descriptions(self, job_ids, max_chars=6000):
        """{job id: plain-text description}. The server caps a call at 48,000 characters shared by
        its ids, so ids go in batches sized to give each close to `max_chars`."""
        ids = [j for j in dict.fromkeys(job_ids) if j]
        batch = max(1, min(20, DESCRIPTION_BUDGET // max(1, max_chars)))
        out = {}
        for start in range(0, len(ids), batch):
            payload = self._call("get-jobs-by-ids", {"jobIds": ids[start:start + batch], "descriptionMaxChars": max_chars})
            for job in payload.get("jobs") or []:
                if job.get("id"):
                    out[job["id"]] = html_to_text(job.get("description"))
        return out

    def fetch_job_description(self, url):
        """-> (status, text). status: success | failed | unsupported | expired."""
        payload = self._call("fetch-job-description", {"url": url})
        return str(payload.get("status") or "failed"), payload.get("jobDescription")

    def list_applications(self, *, filter="all", query=None, limit=25, page=1, include_counts=False):
        args = {"filter": filter, "limit": limit, "page": page, "includeCounts": include_counts}
        if query:
            args["query"] = query
        return self._call("list-applications", args)

    def application_rows(self, pages=5):
        """Tsenta's own most recent application rows (100 per page), newest first."""
        rows = []
        for page in range(1, pages + 1):
            batch = self.list_applications(limit=100, page=page).get("applications") or []
            rows += batch
            if len(batch) < 100:
                break
        return rows

    # ----- applying (costs credits)
    def _require_apply(self):
        if not self.allow_apply:
            raise ApplyBlocked("applying is switched off (DRY_RUN) - nothing was sent to Tsenta")

    def apply_jobs(self, job_ids):
        """Apply to Tsenta feed jobs by id, in chunks of APPLY_CHUNK. Stops early when a chunk
        reports no credits left or an incomplete profile, and says so on the rest."""
        self._require_apply()
        ids = [j for j in dict.fromkeys(job_ids) if j]
        results = []
        for start in range(0, len(ids), APPLY_CHUNK):
            chunk = ids[start:start + APPLY_CHUNK]
            results += self._apply_call({"jobIds": chunk} if len(chunk) > 1 else {"jobId": chunk[0]}, chunk)
            if any(r.stop_run for r in results):
                reason = next(r for r in results if r.stop_run)
                results += [ApplyResult(j, False, reason.category, f"not attempted: {reason.message}") for j in ids[start + APPLY_CHUNK:]]
                break
        return results

    def apply_url(self, url, description=None):
        self._require_apply()
        args = {"url": url}
        if description:
            args["jobDescription"] = description[:10_000]
        return self._apply_call(args, [url])[0]

    def _apply_call(self, args, refs):
        try:
            payload = self._call("apply-to-job", args, retry=False)       # never retried: it could submit twice
        except TsentaToolError as exc:
            message = str(exc)
            return [ApplyResult(r, False, classify_apply_error(message), message) for r in refs]
        except NeedsLogin:
            raise
        except TsentaError as exc:
            return [ApplyResult(r, False, "unknown", f"no answer from Tsenta ({exc}) - check its application list before retrying") for r in refs]
        return parse_apply_response(payload, refs)

    # ----- what really happened
    def wait_for_outcomes(self, application_ids, *, timeout_s=240, poll_s=8, pages=3):
        """Poll Tsenta's own list until every application is settled (or the time is up).
        Returns {application id: Outcome}; anything unsettled stays kind="pending"."""
        wanted = {a for a in application_ids if a}
        outcomes = {}
        deadline = time.monotonic() + timeout_s
        while wanted:
            for page in range(1, pages + 1):
                rows = self.list_applications(limit=100, page=page).get("applications") or []
                for row in rows:
                    if row.get("applicationId") in wanted:
                        outcomes[row["applicationId"]] = outcome_from_row(row)
                if all(a in outcomes for a in wanted) or len(rows) < 100:
                    break
            settled = {a for a, o in outcomes.items() if o.kind != "pending"}
            if wanted <= settled or time.monotonic() >= deadline:
                break
            self._sleep(poll_s)
        return outcomes


# ----------------------------------------------------------------- convenience for the apply queue

def apply_one_url(url, *, outcome_timeout_s=180, client=None):
    """Apply to one posting by URL and wait to hear what Tsenta did. Returns (ok, message) like
    the browser flow. ok is True only for a confirmed send, or one Tsenta accepted and is still
    processing (the message says so); an application waiting on the user's review is reported as
    not sent, with the reason."""
    owned = client is None
    client = client or TsentaClient.from_env(allow_apply=True)
    try:
        if owned:
            client.__enter__()
        balance = client.balance()
        if not balance.has_room:
            return False, f"Tsenta has no applications left (monthly allowance resets {balance.cycle_end or 'later'})"
        description = None
        try:
            status, description = client.fetch_job_description(url)
            if status == "expired":
                return False, "Tsenta says this posting is closed"
        except TsentaError:
            description = None                                             # applying without it is fine
        result = client.apply_url(url, description)
        if not result.success:
            return False, f"Tsenta: {result.message or result.category} [{result.category}]"
        if not result.application_id:
            return True, "Sent to Tsenta (no application id returned, so not confirmed)"
        outcome = client.wait_for_outcomes([result.application_id], timeout_s=outcome_timeout_s).get(result.application_id)
        if outcome is None or outcome.kind == "pending":
            return True, "Sent to Tsenta; it was still processing when last checked"
        if outcome.kind == "applied":
            return True, f"Submitted via Tsenta MCP ({outcome.label or 'sent'})"
        suffix = " Tsenta will retry it by itself." if outcome.will_retry else ""
        return False, f"Tsenta: {outcome.message or outcome.label}.{suffix}".replace("..", ".")
    finally:
        if owned:
            client.__exit__(None, None, None)
