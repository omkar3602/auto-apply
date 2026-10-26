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

Confirmed by a real run (2026-10-08): tsenta can't auto-detect an ATS or
fetch a JD through an indirect redirect link like zapplyjobs' own
`zapply.jobs/l/d/...` apply URLs - it shows a red "We couldn't detect a
supported application system on this page... Choosing the wrong system
will fail" notice instead, and clicking "Apply with Optimized Resume" past
that point does not actually submit anything real (verified against
tsenta's own Applications pipeline - none of 8 such "successful" submits
showed up there). Fixed two ways: `resolve_apply_link` follows a source's
own redirect scheme to the real employer URL before handing it to tsenta
(zapplyjobs' plain HTTP redirect), and `_submit_link` now checks for that
error notice and reports failure instead of blindly clicking Apply and
assuming success.

(A Dice resolver lived here 2026-10-20 through 2026-10-26, including a
logged-in version that clicked the real "Apply Now" button - removed along
with Dice as a source entirely once real testing showed only ~1 in 6
postings even had an external apply link, and the one found in testing
still wasn't a platform tsenta could submit through.)
"""

import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import config as cfg
from automation.browser import get_logged_in_page, dump_debug_snapshot

ADD_OWN_BUTTON_TEXT = "Add your own"
MODAL_HEADING_TEXT = "Add Your Own Link"
FETCH_JD_BUTTON_TEXT = "Fetch JD"
APPLY_BUTTON_TEXT = "Apply with Optimized Resume"
NO_ATS_DETECTED_TEXT = "couldn't detect a supported application system"
JD_WAIT_TIMEOUT_S = 60

ZAPPLY_REDIRECT_HOST = "zapply.jobs"
REDIRECT_RESOLVE_TIMEOUT_S = 15


def _looks_like_dashboard(page):
    return page.get_by_text(ADD_OWN_BUTTON_TEXT, exact=False).count() > 0


def _fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=REDIRECT_RESOLVE_TIMEOUT_S) as resp:
        return resp


def _resolve_zapply_redirect(job_url):
    """zapplyjobs' "Apply" links are a `zapply.jobs/l/d/...` redirect to the
    real employer posting, not the posting itself - a plain HTTP redirect
    tsenta needs the real URL to detect an ATS/fetch a JD. Falls back to the
    original URL on any failure - tsenta will then show its own "couldn't
    detect" notice and _submit_link will catch that, rather than this
    raising and losing the job entirely.
    """
    try:
        return _fetch(job_url).geturl()
    except Exception:
        return job_url


def resolve_apply_link(job_url):
    host = urlparse(job_url).hostname
    if host == ZAPPLY_REDIRECT_HOST:
        return _resolve_zapply_redirect(job_url)
    return job_url


def apply_via_tsenta(job_url):
    """Submits `job_url` to tsenta via the "Add Your Own" flow.
    Returns (ok: bool, message: str). The message always ends with the
    actual link handed to tsenta (post-resolve_apply_link) - for a
    zapplyjobs job this is the resolved real employer URL, not the
    original stored job_url, which otherwise isn't visible anywhere once
    the apply finishes (useful to tell "expired posting, redirect dead-
    ended" apart from "this real URL genuinely isn't a supported ATS").
    """
    resolved_url = resolve_apply_link(job_url)

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
            return False, f"Could not reach/log in to tsenta: {e} | Link used: {resolved_url}"

        try:
            ok, message = _submit_link(page, resolved_url)
        except Exception as e:
            dump_debug_snapshot(page, "tsenta_error")
            ok, message = False, f"tsenta automation failed: {e}"
        finally:
            context.close()

    return ok, f"{message} | Link used: {resolved_url}"


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

    if scope.get_by_text(NO_ATS_DETECTED_TEXT, exact=False).count() > 0:
        return False, (
            "tsenta couldn't detect a supported application system for this "
            "URL - not actually submitted"
        )

    scope.get_by_text(APPLY_BUTTON_TEXT, exact=False).first.click()
    page.wait_for_timeout(2000)
    dump_debug_snapshot(page, "tsenta_final")

    if scope.get_by_text(NO_ATS_DETECTED_TEXT, exact=False).count() > 0:
        return False, (
            "tsenta still showed 'no supported application system' after "
            "clicking Apply - not actually submitted"
        )

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
