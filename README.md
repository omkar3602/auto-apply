# Auto Apply

Local Flask app that pulls job listings from multiple boards into a database,
shows them as cards you can Pass or Apply on (one tab per board), and hands
"Apply" off to tsenta.com's "Add Your Own" flow via browser automation.

Job boards wired up so far:
- **JobRight** (`scrapers/jobright.py`) — scrapes jobright.ai's logged-in
  recommend feed via Playwright (Google SSO login required, see below).
- **Simplify New Grad** (`scrapers/simplify.py`) — reads the structured
  `listings.json` published by
  [SimplifyJobs/New-Grad-Positions](https://github.com/SimplifyJobs/New-Grad-Positions)'s
  bot. No login, no browser automation - just a plain HTTP fetch. Currently
  ~3,100 active listings across all categories (Software, Hardware, Quant,
  etc.) - re-syncing is cheap so there's no harm clicking it often, but expect
  the tab to be long.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chrome
```

## Run

```bash
python app.py
```

Visit http://localhost:5000 — it redirects to the JobRight tab.

## First run: logging in

Both jobright.ai and tsenta.com sit behind Google SSO, which can't be driven
headlessly. The first time you click **Sync Jobs** (or **Apply** on a card), a
real Chrome window will pop up on your machine — log in with Google in that
window like normal. The session is saved to `browser_profiles/jobright/` (or
`browser_profiles/tsenta/`) and reused headlessly on every run after that. If
a saved session ever expires, the same headed-login flow kicks in again
automatically.

Google actively blocks sign-in from browsers it detects as automated
("This browser or app may not be secure"). To get past that, the app drives
real installed Chrome (Playwright's `chrome` channel, not the bundled
Chromium) and strips the most common automation tells (`navigator.webdriver`,
the `--enable-automation` flag). If Google still rejects the sign-in even
with that: close the window, delete the relevant folder under
`browser_profiles/` to start with a completely clean profile, and try again —
a profile that's already been flagged tends to keep getting flagged.

## If jobright.ai or tsenta.com selectors ever drift

`scrapers/jobright.py` and `automation/tsenta.py` are matched against real
captured DOM (not guessed), using stable class-name/id prefixes rather than
full hashed values so a redeploy that only changes hashes shouldn't break
them. If a redeploy changes actual markup/labels and something stops
working: every Playwright call in both files (and in `automation/browser.py`)
runs through `dump_debug_snapshot()`, which writes an HTML + screenshot pair
to `debug/` at each step - `debug/jobright_*.png` / `debug/tsenta_*.png` show
exactly what the browser saw, so a fix can be aimed rather than guessed.
`scrapers/simplify.py` has no such fragility - it's just JSON.

## Data model

Single `jobs` table (`models.py`). `status` drives everything:
- `new` / `applying` / `apply_failed` → shown on the source's tab
- `passed` / `applied` → shown on `/history`

## Adding another job board later

1. Add a scraper module under `scrapers/` with a `sync_jobs(app)` function
   that upserts `Job` rows with a new `source` value (see `scrapers/jobright.py`).
2. Register it in `scrapers/__init__.py`'s `SOURCES` dict — it automatically
   gets a nav tab, sync button, and its own locked background worker.

## Using it from your phone (Tailscale)

The app is installable as a PWA and its layout is phone-friendly. The only
missing piece is reachability: your phone needs an HTTPS URL pointing at the
app (a PWA won't install over plain HTTP unless it's localhost).

Tailscale solves that without hosting anything. It puts your Mac and phone on
a private network and gives the Mac a real HTTPS `*.ts.net` address that only
your own devices can reach. **The app itself doesn't change at all** — it
keeps running on your Mac, right next to the `browser_profiles/` login
sessions Sync and Apply depend on.

### Setup

1. **Install Tailscale on the Mac** and sign in (any of Google/GitHub/etc.):
   ```bash
   brew install --cask tailscale
   ```
   or download from [tailscale.com/download](https://tailscale.com/download).

2. **Install Tailscale on your phone** (App Store / Play Store) and sign in
   with the *same* account, so both devices join the same tailnet.

3. **Enable HTTPS for your tailnet** — Tailscale admin console →
   DNS → enable MagicDNS, then enable HTTPS Certificates. This is what makes
   the `*.ts.net` cert possible.

4. **Start the app**, then put Tailscale Serve in front of it:
   ```bash
   python app.py                      # terminal 1 - listens on :8000
   tailscale serve --bg 8000          # terminal 2 - proxies HTTPS -> :8000
   tailscale serve status             # prints your https://<mac>.<tailnet>.ts.net URL
   ```

5. **Open that URL on your phone**, then use Share → "Add to Home Screen"
   (iOS Safari) or the install prompt (Android Chrome).

To stop sharing: `tailscale serve --https=443 off`.

### Notes and caveats

- **Your Mac must be awake** for anything to work — it's serving the app, not
  just the automation. Worth checking System Settings → Battery/Lock Screen
  if it sleeps too eagerly.
- **Nothing is public.** Only devices signed into your tailnet can reach the
  URL, which is why there's no login screen — adding one is possible but
  redundant here.
- **Known iOS issue:** [tailscale#19147](https://github.com/tailscale/tailscale/issues/19147)
  reports Serve HTTPS endpoints failing on iPhone with an SSL error. It may
  be stale or situational — test step 5 early. If it does bite, the
  `hosted-fly-agent` branch has a complete, tested Fly.io deployment as a
  fallback (see its `BRANCH-NOTES.md`).
- The service worker deliberately does **not** register on
  `localhost`/`127.0.0.1`, so local development never picks up PWA caching.
