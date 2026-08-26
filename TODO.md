# TODO

## Pending
- [ ] **Phone access via Tailscale** (in progress). Install Tailscale on the Mac + phone, `tailscale serve` the local Flask app to get an HTTPS `*.ts.net` URL, then install the PWA from it. Nothing to build - the app stays exactly as-is, it just becomes reachable. See README "Using it from your phone (Tailscale)".
  - Known risk to check first: [tailscale#19147](https://github.com/tailscale/tailscale/issues/19147) reports iOS failing on Serve HTTPS endpoints. If that bites, fall back to the `hosted-fly-agent` branch (built + tested, just never deployed).
- [ ] LinkedIn as a source - explicitly deferred, not re-planned.

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
