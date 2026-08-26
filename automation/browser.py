"""Shared Playwright helpers: persistent per-site browser profiles with a
"log in by hand once, reuse the session forever after" flow.

Both jobright.ai and tsenta.com sit behind Google SSO, which Playwright can't
drive headlessly (Google blocks automated login flows). So instead: launch a
persistent context (cookies saved to disk under browser_profiles/<site>/).
Try headless first - if `is_logged_in(page)` never returns True (we're on a
login wall), relaunch the same profile headed so a human can finish the
Google login in the window that pops up, and keep using that headed context
for the rest of the run. Every run after that succeeds headless from step 1.

Google's sign-in page actively rejects automated browsers ("This browser or
app may not be secure") - Playwright's default bundled Chromium is fingerprinted
as automated (navigator.webdriver=true, missing Chrome branding, etc). We use
the `chrome` channel (real Google Chrome, run `playwright install chrome`
once) plus a couple of the standard automation-tell removals below to get
past that.
"""

import os

import config as cfg

HEADLESS_LOGIN_CHECK_TIMEOUT_S = 15
HEADED_LOGIN_WAIT_TIMEOUT_S = 300
POLL_INTERVAL_MS = 500
DEBUG_DUMP_EVERY_MS = 5_000

DEBUG_DIR = os.path.join(cfg.BASE_DIR, "debug")

_STEALTH_ARGS = ["--disable-blink-features=AutomationControlled"]
_STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"


def dump_debug_snapshot(page, label):
    """Writes the current page's HTML + a screenshot to debug/<label>.{html,png}
    so we can see what the browser is actually showing without needing eyes
    on the machine. Best-effort - never raises.
    """
    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        with open(os.path.join(DEBUG_DIR, f"{label}.html"), "w") as f:
            f.write(page.content())
        page.screenshot(path=os.path.join(DEBUG_DIR, f"{label}.png"), full_page=True)
    except Exception:
        pass


def _launch(playwright, profile_dir, headless):
    context = playwright.chromium.launch_persistent_context(
        profile_dir,
        headless=headless,
        channel="chrome",
        args=_STEALTH_ARGS,
    )
    context.add_init_script(_STEALTH_INIT_SCRIPT)
    return context


def _wait_until_logged_in(page, is_logged_in, timeout_s, debug_label=None):
    elapsed_ms = 0
    timeout_ms = timeout_s * 1000
    since_dump_ms = 0
    while elapsed_ms < timeout_ms:
        try:
            if is_logged_in(page):
                if debug_label:
                    dump_debug_snapshot(page, debug_label)
                return True
        except Exception:
            pass
        if debug_label and since_dump_ms >= DEBUG_DUMP_EVERY_MS:
            dump_debug_snapshot(page, debug_label)
            since_dump_ms = 0
        page.wait_for_timeout(POLL_INTERVAL_MS)
        elapsed_ms += POLL_INTERVAL_MS
        since_dump_ms += POLL_INTERVAL_MS
    return False


def get_logged_in_page(playwright, profile_dir, url, is_logged_in,
                        login_timeout=HEADED_LOGIN_WAIT_TIMEOUT_S, debug_label=None):
    """Returns (context, page) navigated to `url` once `is_logged_in(page)`
    returns True. `is_logged_in` is a callable(page) -> bool - use something
    specific enough that it can't accidentally match a logged-out/marketing
    page (e.g. require more than one distinctive element).

    If `debug_label` is given, the page's HTML/screenshot are dumped to
    debug/<label>_headless.*, debug/<label>_headed.* every few seconds while
    waiting, and debug/<label>_final.* on success - so the actual DOM at any
    point in the flow can be inspected after the fact.

    Caller owns the context and must call context.close().
    """
    context = _launch(playwright, profile_dir, headless=True)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    if debug_label:
        dump_debug_snapshot(page, f"{debug_label}_headless")

    if _wait_until_logged_in(page, is_logged_in, HEADLESS_LOGIN_CHECK_TIMEOUT_S):
        if debug_label:
            dump_debug_snapshot(page, f"{debug_label}_final")
        return context, page

    # Not logged in - fall back to a headed window so a human can complete
    # Google SSO by hand, using the same persistent profile.
    context.close()

    context = _launch(playwright, profile_dir, headless=False)
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded")
    if debug_label:
        dump_debug_snapshot(page, f"{debug_label}_headed")
    if not _wait_until_logged_in(page, is_logged_in, login_timeout, debug_label=f"{debug_label}_headed" if debug_label else None):
        context.close()
        raise TimeoutError(
            f"Gave up waiting for login at {url} after {login_timeout}s"
        )
    if debug_label:
        dump_debug_snapshot(page, f"{debug_label}_final")
    return context, page
