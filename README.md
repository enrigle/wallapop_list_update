# Wallabump

Toggles one character in every active listing so items resurface in the feed. Emails a receipt after every run.

**Schedule:** one site per night at 22:25 — Wallapop Thu + Sun, Vinted Fri + Mon. Each listing still gets bumped twice a week.

Sites are config, not code: `sites.toml` holds one section per marketplace. Wallapop and Vinted are set up; a third is a config entry.

No LLM, no API key, no cloud. One dependency (`playwright`), driving your real Chrome at human pace.

> Wallapop's Terms of Service prohibit automated access. This drives your own account against your own listings, but the risk of account action is real. Your call.

---

## Commands

Prefix everything with the venv: `.venv/bin/python bump.py …`

| Command | Does |
| --- | --- |
| `status` | Signed in? When is the next run? Changes nothing. |
| `login` | Open Chrome, sign in. Once, and after `RE-AUTH NEEDED`. |
| `run` | Dry run. Changes nothing. |
| `run --publish` | The real thing. What launchd runs. |
| `run --publish --limit 1` | Real, one listing per site. Smoke test. |
| `probe` | Dump catalog HTML when selectors break. |
| `stats` | Snapshot Wallapop views/chats/favourites to CSV. Read-only. |
| `analyze` | Crossover experiment result from the stats CSVs. |

Add `--site vinted` for one site, `--experiment` to bump only half, `-v` for debug logging.

---

## Setup

### 1. Environment

```bash
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

System `python3` is anaconda 3.8.5, too old. No `playwright install` needed — it attaches to the Chrome in `/Applications`.

### 2. Gmail credentials

```bash
security add-generic-password -a you@gmail.com -s wallabump-gmail -w
```

Use a Gmail [app password](https://myaccount.google.com/apppasswords), not your account password. Nothing else stores a credential.

### 3. Point Vinted at your wardrobe

`catalog_url` under `[vinted]` in `sites.toml` has your member id in it. Vinted → **Armario** → copy the URL. Wallapop needs no edit.

### 4. Sign in

```bash
.venv/bin/python bump.py login
```

Opens Chrome on `~/.wallapop-bump/chrome-profile/`. Sign in to every site, press Enter. It names any still signed out, then closes Chrome.

### 5. Test

```bash
.venv/bin/python bump.py run                    # dry, changes nothing
.venv/bin/python bump.py run --publish --limit 1  # one listing per site
```

### 6. Schedule

```bash
cp com.enrigle.wallabump.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.wallapop.plist
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.vinted.plist
launchctl start com.enrigle.wallabump.wallapop   # force one run now
```

One agent per site, and they must never share a weekday — a test enforces that. Force a run: it is where absolute-path bugs surface.

**First forced run may hang silently.** macOS is asking a launchd-spawned Python for Documents access via a dialog, possibly offscreen. Click Allow, once.

Editing a plist later needs `launchctl unload`, `cp`, `launchctl load`. A reload kills a run in progress.

The Mac must be logged in to your GUI session. launchd agents and Chrome need one.

---

## Checking on it

```text
Chrome    running on http://127.0.0.1:9222
wallapop  signed in  (22 listings)
vinted    signed in  (20 listings)

Last run  2026-09-10 14:55  Wallabump: 2 ok, 0 skipped, 0 unverified, 0 failed
Next run  wallapop  Thu 22:25  (in 1d 0h)
Next run  vinted    Fri 22:25  (in 2d 0h)
```

Exits 0 when every site is signed in, 1 otherwise, so it works as a check. Counts listings with the same collector a real run uses, so the numbers cannot disagree. Takes ~30s.

Last run is read from the log's own `Done —` line; next run from each site's installed plist. No state file anywhere.

---

## How it works

1. **Attach.** Checks `127.0.0.1:9222`, launches Chrome if dead, thaws every tab, connects over CDP.
2. **Collect.** Scrapes the live catalogue. Reserved and sold are skipped.
3. **Toggle.** Per listing: open edit form, flip the trailing period, save.
4. **Verify.** Reopen the form and compare. Mismatch → `unverified`.
5. **Report.** One email. Exit 0 clean, 1 some failures, 2 fatal.

Between listings it sleeps 20–90s at random, and `caffeinate -i` runs for the whole job.

**No state file.** Every run reads what is actually there. New listings are picked up automatically, and a half-finished run self-heals on the next one.

**A sleeping Mac aborts instead of limping.** Only a suspended process sees the wall clock and the monotonic clock diverge, so a gap over a minute proves the Mac slept rather than the step being slow.

**One site failing does not stop the others.** Only a dead browser aborts everything.

---

## Why a separate Chrome

Wallapop's bot protection forces a login screen on any *Playwright-launched* Chrome, even with a valid cookie. So the script launches a plain, non-automated Chrome on its own profile and attaches over CDP. That looks like a human reopening Chrome.

**Frozen tabs are thawed first.** Chrome freezes idle background tabs, and a frozen tab answers no CDP command — not slowly, never. Attaching adopts *every* tab and waits for each, so one frozen tab stalls the whole handshake while the browser itself answers in milliseconds. Each run activates every tab over the HTTP endpoint first.

**Chrome closes when the command ends** — but only one the script opened. While it runs, clicking Chrome in the Dock opens *that* one, not your normal Chrome. Left running, it quietly becomes your everyday browser, and every personal tab in it is one more tab to freeze.

**No `pmset` wake, deliberately.** A scheduled wake on this Mac is a 4–6 second DarkWake, on battery *and* on mains, even for a user-visible alarm. A 25-minute catalogue edit cannot run in 6-second slices. 22:25 works because the laptop is usually awake then. If it is asleep, nothing runs and nothing breaks: launchd fires the missed job on the next wake.

**Security note:** the debugging port is local-only, but any process on this Mac could attach to it while Chrome is open.

---

## The email

```text
Wallabump: 12 ok, 1 skipped, 0 unverified, 0 failed

wallapop  ok       camiseta-fcb-mujer-nike  — added '.' → …'en las fotos.'
vinted    skipped  jersey-lana-talla-m      — description unsafe to toggle
```

Direction alternates run to run. Both directions bump equally — an edit is an edit.

**Silence is the failure signal.** No email means the run never happened; check `~/.wallapop-bump/bump.log`.

| Status | Meaning |
| --- | --- |
| `ok` | Edited, verified by reopening the form. |
| `dry-run` | Would edit; `--publish` not passed. |
| `skipped` | Reserved, sold, control group, or unsafe to toggle. |
| `unverified` | Save clicked, text did not change. Investigate. |
| `failed` | Edit form not found, or browser error. |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| No email at all | Run never fired. Mac asleep, or agent not loaded. | `launchctl list \| grep wallabump`, check `bump.log`. |
| `RE-AUTH NEEDED` | A session expired. Body says which site. | `bump.py login` |
| Every item `failed` | Site redesigned, selectors stale. | `bump.py probe`, fix `sites.toml`. ~15 min. |
| Run hangs at startup, no log | macOS privacy dialog waiting offscreen. | Find it, click Allow. Once. |
| `Browser connection lost mid-run` | Chrome died, or the Mac slept. | Re-run. |
| `Mac slept for N min mid-run` | Asleep or on a DarkWake. Usually battery plus a closed lid. | Nothing to fix; the next run covers everything. |
| `CDP handshake did not finish` | A frozen tab would not thaw. | Quit that Chrome and re-run. Not a login problem. |
| Many `unverified` | Save button selector matches the wrong control. | Check `save_control` against a `probe` dump. |

`probe` dumps `~/.wallapop-bump/probe-<site>.html` per site and reports how many listing links each shows. Fix that site's section in `sites.toml` — no code change. Expect this once or twice a year per site.

---

## Measuring whether it works

Does the edit actually resurface a listing? A **crossover** experiment answers it for Wallapop:

- `--experiment` (on in the Wallapop agent) bumps half the catalogue per ISO week. The halves swap every week, so each listing is compared against itself.
- `com.enrigle.wallabump.stats.plist` snapshots Wallapop's weekly stats daily at 20:00 into `~/.wallapop-bump/stats/`. Read-only.
- `bump.py analyze` uses Monday snapshots. Each Monday's figure covers the week that ended. Output: per-listing views lift (bumped weeks − control weeks), mean lift, exact sign-flip p-value.

**Window:** ISO weeks 42–45 (Mon 12 Oct → Sun 8 Nov 2026). Run `analyze` after the Mon 9 Nov snapshot.

**During the window:** never run Wallapop with `--publish` but no `--experiment`. That bumps the control half and ruins the comparison.

```bash
cp com.enrigle.wallabump.stats.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.stats.plist
.venv/bin/python bump.py analyze
```

Caveats: ~15 listings × 4 weeks only shows a large effect. A null result means "no large effect", not "no effect". Last week's bump may linger into this week's control week, so the lift is underestimated.

---

## What it edits

A trailing period, toggled on and off. Never title, price, photos or category.

It refuses descriptions that are empty, end in `..`/`...`, end in other punctuation (`?`, `!`, `,`, `:`), or are already at `maxlength` — each would publish visible garbage.

To use something other than a period, change `MARKER` at the top of `bump.py`. `toggle()` and `describe()` both read it.

---

## Development

```bash
.venv/bin/python -m pytest -x
.venv/bin/ruff format . && .venv/bin/ruff check . --fix
.venv/bin/mypy . --strict
```

Only the pure parts are tested — `toggle()`, `describe()`, the config layer, the A/B split, the schedule plists. The rest is browser I/O, verified by dry run.

| File | Holds |
| --- | --- |
| `bump.py` | Entry point shim. Same as `python -m wallabump`. |
| `wallabump/__main__.py` | CLI and logging setup. |
| `wallabump/logic.py` | Pure logic: the toggle, schedules, site config, experiment math. |
| `wallabump/chrome.py` | The bot's Chrome: launch, close, thaw tabs, CDP attach. |
| `wallabump/pages.py` | Page actions: find listings, edit one, read stats. |
| `wallabump/report.py` | Run results and the receipt email. |
| `wallabump/commands.py` | `run`, `login`, `status`, `probe`, `stats`, `analyze`. |
| `sites.toml` | Per-site URLs and selectors. Edit when a site redesigns. |
| `test_bump.py` | Edge cases for the pure functions. |
| `com.enrigle.wallabump.<site>.plist` | One launchd agent per site. Copy to `~/Library/LaunchAgents/`. |
| `~/.wallapop-bump/` | Chrome profile, log, probe dumps, stats CSVs. Never in the repo. |
