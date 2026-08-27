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

Visit http://localhost:8000 — it redirects to the JobRight tab.

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

- **Works from anywhere** — cellular, hotel wifi, another country. Tailscale
  connects your devices over the internet, not just the local network.
- **But your Mac must be awake and online**, because it's serving the app,
  not just running the automation. If it stays home plugged in:
  ```bash
  sudo pmset -c sleep 0          # never idle-sleep while on charger
  # or, just for one session:
  caffeinate -s python app.py    # awake only while the app runs
  ```
  On a MacBook, closing the lid sleeps it regardless of these settings
  (unless it's in clamshell mode with an external display) — so leaving it
  home *closed* won't work.
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

## Keeping it running in the background

Two separate things get confused here:

- **Locking your screen does not stop the app.** Neither does the display
  going dark. Processes keep running.
- **The system sleeping does stop it.** That's what actually needs
  preventing — and it only applies when the Mac is idle *and* you haven't
  told it otherwise.

Running `python app.py` in a terminal also dies when you close that window
or log out. The fix for both is a `launchd` agent, which is macOS's native
way to run something in the background.

Use the `./autoapply` script — one command, no `launchctl` incantations:

```bash
./autoapply start        # install + start; survives screen lock and logout
./autoapply status       # running? on what PID? responding? phone URL?
./autoapply restart      # pick up code changes
./autoapply logs         # tail output + error logs
./autoapply stop
./autoapply uninstall    # stop and remove the agent
```

`start` installs a `launchd` agent, so the app comes back at login and
restarts itself if it ever crashes. `status` prints something like:

```
service:  running (pid 46280)
http:     responding on http://127.0.0.1:8000
phone:    https://your-mac.your-tailnet.ts.net
```

The agent wraps the app in `caffeinate -s`, which holds off system sleep
**only while the app is running, and only on AC power** — precisely the
"plugged in, screen locked" case. No permanent `pmset` change needed, and
the moment the service stops your Mac sleeps normally again. Your display
still sleeps and your screen still locks as usual.

### Running it from anywhere

Symlink it into a folder that's on your `PATH`:

```bash
ln -sfn "$PWD/autoapply" ~/bin/autoapply      # or wherever you keep scripts
```

Then `autoapply start` works from any directory. **Symlink rather than
copy** — the script follows symlinks back to the repo to locate the
project, and a copy silently goes stale the moment the script changes.

If you do copy it, it falls back to the `DEFAULT_REPO` path baked in at the
top of the script. To point it somewhere else without editing:

```bash
export AUTOAPPLY_HOME=/path/to/auto-apply
```

`autoapply status` prints which project it resolved to, so you can always
check what it's controlling.

### If the port is already taken

Running the app by hand *and* as a service will collide — only one can hold
port 8000. `start` detects this and tells you which process to stop rather
than failing silently:

```
error: port 8000 is already in use by pid 44054:
    .../Python app.py
```

Stop the manual one (Ctrl+C in its terminal), then `./autoapply start`.
Or run the service somewhere else: `PORT=8001 ./autoapply start`.

### Caveats

- **The lid still wins.** Closing a MacBook's lid sleeps it regardless of
  `caffeinate` (unless clamshell mode with an external display). Leave it
  open.
- **This project lives on `/Volumes/Personal`.** If that disk isn't mounted
  at login the agent will fail and retry every 30s (`ThrottleInterval`),
  recovering on its own once it mounts. Check the error log if the app
  seems missing after a reboot.
- **Tailscale Serve config persists on its own** — `tailscale serve --bg`
  is stored by the Tailscale daemon and survives reboots, so it doesn't
  need an agent of its own.
- **Debug mode is off by default** (see `app.py`). That matters here:
  Tailscale Serve exposes the app to your other devices, and Flask's
  interactive debugger can execute code. Tracebacks still land in
  `/tmp/autoapply.err.log`. For the in-browser debugger while developing
  locally, run `AUTO_APPLY_DEBUG=true python app.py` by hand instead.
