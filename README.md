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
