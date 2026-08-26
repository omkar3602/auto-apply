"""Drives tsenta.com's "Add Your Own" flow so tsenta takes over the actual
job application.

Confirmed flow (from screenshots of the real UI):
1. https://dashboard.tsenta.com/dashboard
2. Click the "+ Add your own" pill button
3. A modal titled "Add Your Own Link" opens with a "Job URL" input and a
   "Fetch JD" button next to it, a "Job Description" textarea below (JD is
   optional - a caption says you can "apply without it"), and a bottom
   "Apply with Optimized Resume ->" button.
4. Paste the job URL, click "Fetch JD", give it a while to populate the JD
   textarea, then click "Apply with Optimized Resume".

Every step dumps a debug snapshot (debug/tsenta_*.png/html) so a failure at
any point can be inspected without re-running blind.
"""

from playwright.sync_api import sync_playwright

import config as cfg
from automation.browser import get_logged_in_page, dump_debug_snapshot

ADD_OWN_BUTTON_TEXT = "Add your own"
MODAL_HEADING_TEXT = "Add Your Own Link"
FETCH_JD_BUTTON_TEXT = "Fetch JD"
APPLY_BUTTON_TEXT = "Apply with Optimized Resume"
JD_WAIT_TIMEOUT_S = 60


def _looks_like_dashboard(page):
    return page.get_by_text(ADD_OWN_BUTTON_TEXT, exact=False).count() > 0


def apply_via_tsenta(job_url):
    """Submits `job_url` to tsenta via the "Add Your Own" flow.
    Returns (ok: bool, message: str).
    """
    with sync_playwright() as p:
        try:
            context, page = get_logged_in_page(
                p,
                cfg.TSENTA_PROFILE_DIR,
                cfg.TSENTA_DASHBOARD_URL,
                is_logged_in=_looks_like_dashboard,
                debug_label="tsenta",
            )
        except Exception as e:
            return False, f"Could not reach/log in to tsenta: {e}"

        try:
            return _submit_link(page, job_url)
        except Exception as e:
            dump_debug_snapshot(page, "tsenta_error")
            return False, f"tsenta automation failed: {e}"
        finally:
            context.close()


def _modal_scope(page):
    """Everything after clicking "Add your own" happens inside a dialog -
    scope to it if we can find one, so we don't accidentally hit an
    unrelated input/button elsewhere on the dashboard.
    """
    dialog = page.locator('[role="dialog"]')
    if dialog.count() > 0:
        return dialog.first
    return page


def _submit_link(page, job_url):
    page.get_by_text(ADD_OWN_BUTTON_TEXT, exact=False).first.click()
    page.wait_for_selector(f"text={MODAL_HEADING_TEXT}", timeout=10_000)
    dump_debug_snapshot(page, "tsenta_modal_opened")

    scope = _modal_scope(page)

    job_url_input = scope.locator("input").first
    job_url_input.fill(job_url)

    scope.get_by_text(FETCH_JD_BUTTON_TEXT, exact=False).first.click()
    dump_debug_snapshot(page, "tsenta_after_fetch_jd_click")

    jd_textarea = scope.locator("textarea").first
    _wait_for_jd(page, jd_textarea, JD_WAIT_TIMEOUT_S)
    dump_debug_snapshot(page, "tsenta_after_jd_wait")

    scope.get_by_text(APPLY_BUTTON_TEXT, exact=False).first.click()
    page.wait_for_timeout(2000)
    dump_debug_snapshot(page, "tsenta_final")
    return True, "Submitted to tsenta"


def _wait_for_jd(page, jd_textarea, timeout_s):
    """Polls the Job Description textarea until it's populated (Fetch JD
    finished) or the timeout passes. JD is optional per tsenta's own UI copy
    ("apply without it"), so a timeout here isn't fatal - we just proceed.
    """
    elapsed = 0
    while elapsed < timeout_s:
        try:
            if jd_textarea.input_value().strip():
                return True
        except Exception:
            pass
        page.wait_for_timeout(1000)
        elapsed += 1
    return False
