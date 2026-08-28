# TODO

## Pending
- [ ] Add Linkedin reach out feature (different from current use-case)

## Parked
- Fly.io hosted deployment + local automation agent: branch `hosted-fly-agent`, see its `BRANCH-NOTES.md`. Complete and locally tested (agent round-trip, auth, gunicorn without Playwright), never deployed. Parked because Tailscale keeps the backend on the Mac and makes the whole agent split unnecessary.

## Done
- [x] PWA basics on master: manifest, service worker (only registers on a real hosted domain, never localhost), icons, mobile-responsive layout (no horizontal overflow at 360/390px, full-width cards, larger touch targets)
- [x] Flask app scaffolded: models, routes, templates, background sync/apply workers
- [x] JobRight tab: Playwright scraper against real DOM, virtualization-aware, 0-2-day-old filter, real employer application URL (not jobright's own page), composed JD from structured detail-page JSON
- [x] Simplify New Grad tab: reads `listings.json` directly, 0-2-day-old filter
- [x] tsenta Apply automation: dashboard -> "+ Add your own" -> Job URL -> Fetch JD -> Apply with Optimized Resume, confirmed live
- [x] Pass / Apply / Details / History pages
- [x] Job board badge (Greenhouse/Workday/Oracle Cloud/etc., "Unknown" fallback)
- [x] Application Link on the card itself
- [x] Job Description shown on detail page
- [x] Loading UI: spinners + overlays for Sync and Apply, sync errors surfaced instead of silent
- [x] **Phone access via Tailscale** (in progress). Install Tailscale on the Mac + phone, `tailscale serve` the local Flask app to get an HTTPS `*.ts.net` URL, then install the PWA from it. Nothing to build - the app stays exactly as-is, it just becomes reachable. See README "Using it from your phone (Tailscale)".
  - Known risk to check first: [tailscale#19147](https://github.com/tailscale/tailscale/issues/19147) reports iOS failing on Serve HTTPS endpoints. If that bites, fall back to the `hosted-fly-agent` branch (built + tested, just never deployed).
- [x] Swipe mode: Tinder-style card stack (drag + flick to pass/apply, velocity-based swipe detection, axis-locked drag so scrolling the description doesn't get mistaken for a swipe), full-height responsive card sizing on phone, undo button for the last pass, set as the app's default landing page
- [x] Internal apply queue: Apply / right-swipe just marks a job "queued" instead of spawning a thread per click; a single background worker drains it into tsenta one at a time in order, with startup recovery for any job left stuck mid-"applying" by a crash or restart
- [x] Server timezone: background service runs with `TZ=America/New_York`; History page timestamps convert from stored UTC instead of displaying raw UTC
- [x] Job board visibility: solid badge styling (was a faint outline), distinct styling for "Unknown", Board column added to History
- [x] Simplify job descriptions: scraped from the real application link (Workday/Greenhouse/Lever/Ashby/iCIMS/SmartRecruiters/Workable/BambooHR selectors + generic fallback) since `listings.json` has none; falls back to "no description available" on failure
- [x] **Daily apply reminder** via Web Push at 11:00 AM America/New_York (DST-safe). Self-generated VAPID keypair (`instance/vapid_private.pem`, auto-created on first run, gitignored) - no third-party push provider account needed. Bell button in the topbar (hidden on localhost, same gate as the service worker) requests notification permission and subscribes; a single background thread sleeps until the next 11:00 AM and pushes to every stored subscription, pruning ones the push service reports gone (404/410).
  - **Confirmed end-to-end on a real device**: fired a live test push that reached the iPhone PWA via Apple's web.push.apple.com. Fixed three real bugs surfaced along the way:
    1. Service worker registered from `/static/sw.js` defaulted to scope `/static/` instead of `/`, so it never controlled real pages and `serviceWorker.ready` (which subscribe/unsubscribe depend on) never resolved. Now served from `/sw.js` at the root.
    2. `app.logger.info(...)` was silently dropped app-wide (no logging config set a handler/level) - added `logging.basicConfig(level=logging.INFO)`, so sync/apply/reminder results are now actually visible in the log.
    3. Apple's push service rejects the VAPID `sub` claim if it looks fake (`mailto:...@localhost`) with `403 BadJwtToken` - Chrome/FCM didn't care, Apple did. Set via `VAPID_CONTACT_EMAIL` in the `autoapply` plist (same mechanism as `PORT`/`TZ`), not hardcoded into committed source.
  - **Known gotcha for future env var changes**: `./autoapply restart` calls `launchctl kickstart -k`, which restarts the process but does **not** make launchd re-read the plist file - env var changes silently don't take effect until a full `./autoapply stop && ./autoapply start` (or `uninstall` + `start`). Bit us mid-testing; worth fixing in the script itself at some point.
