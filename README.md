# Wallabump

Twice week (Wed + Sat, 10:00), toggle one char in every active Wallapop listing so items resurface in feeds. Emails receipt after every run.

No LLM, no API key, no cloud. One dependency (`playwright`), driving your real Chrome from your machine at human pace.

> Wallapop's Terms of Service prohibit automated access. This drives your own account against your own listings and is the lowest-signature approach available, but the residual risk of account action is real. Your call.

## Contents

- [Commands](#commands) — cheat sheet
- [How it works](#how-it-works) — the run, start to finish
- [Why a separate Chrome](#why-a-separate-chrome) — the one non-obvious design choice
- [Setup](#setup) — seven steps, once
- [Daily life](#daily-life) — reading the email
- [Troubleshooting](#troubleshooting) — symptom, cause, fix
- [What it edits](#what-it-edits) — and what it refuses to
- [Development](#development)

---

## Commands

| Command | Does |
| --- | --- |
| `bump.py login` | Open Chrome, sign in, confirm session. Once, and after `RE-AUTH NEEDED`. |
| `bump.py run` | Dry run. Reads everything, changes nothing. |
| `bump.py run --publish` | The real thing. What launchd runs. |
| `bump.py run --publish --limit 1` | Real, one listing. Smoke test. |
| `bump.py probe` | Dump catalog HTML when selectors break. |
| `-v` | Debug logging on any of the above. |

All take the venv interpreter: `.venv/bin/python bump.py …`

---

## How it works

1. **Attach.** Checks `127.0.0.1:9222`. Dead → launches plain Chrome on the saved profile, waits up to 15s.
2. **Stay awake.** Spawns `caffeinate -i` for the run's lifetime. A full catalog takes ~25 min and idle sleep kills the connection.
3. **Collect.** Scrapes the live catalog page for `/item/` links. Reserved and sold are marked skipped.
4. **Toggle.** Per listing: open edit form, read description, flip trailing period, save.
5. **Verify.** Reopens the form and compares. Mismatch → `unverified`.
6. **Pace.** Sleeps 20 to 90s between listings, random.
7. **Report.** Emails every line, exits 0 (clean), 1 (some failures), or 2 (fatal).

**No state file.** Every run reads what is actually there and decides fresh. So a new Wallapop listing is included automatically, a half-finished run self-heals on the next one, and a failed listing is retried like any other. Nothing tracks "which ones failed last time" because nothing needs to.

**Connection loss aborts.** If Chrome dies mid-run, the run stops and emails one fatal line rather than 12 identical `TargetClosedError` failures.

---

## Why a separate Chrome

Wallapop bot protection forces re-login screen on any *Playwright-launched* Chrome — even one holding valid session cookie in profile dir. Headless and headed both hit this.

Workaround: script starts plain, non-automated Chrome on own profile (`~/.wallapop-bump/chrome-profile/`), or reuses running one, attaches over Chrome DevTools Protocol (CDP). Plain launch look like human reopening Chrome, so re-login trigger no fire.

**This means:** quitting that Chrome or rebooting Mac fine. Next run relaunches from saved profile. If Wallapop asks login anyway, run emails `RE-AUTH NEEDED`; run `bump.py login` again.

**Security note:** the debugging port (`127.0.0.1:9222`) only accepts local connections, but any other process on this Mac could in principle attach to it too while Chrome is running with it open. Acceptable on a personal machine, worth knowing.

---

## Setup

### 1. Environment

```bash
cd /Users/enrigle/Documents/code/wallapop
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

System default `python3` on this Mac is anaconda 3.8.5 — too old. Use Homebrew 3.14 above.

No `playwright install` needed: script attaches to Google Chrome already in `/Applications`, not downloaded Chromium.

### 2. Gmail credentials in the Keychain

One entry holds both address and app password:

```bash
security add-generic-password -a you@gmail.com -s wallabump-gmail -w
```

Prompts for password. Use Gmail **app password** (<https://myaccount.google.com/apppasswords>), not account password. Reports sent from that address to itself.

Nothing else stores a credential. No secret ever lands in the repo.

### 3. Sign in once

```bash
.venv/bin/python bump.py login
```

Opens real Chrome (not automated) on profile at `~/.wallapop-bump/chrome-profile/`, mode 700. Sign in normally — Google account, SMS/email code, whatever asks. Once listings visible, back to terminal and press Enter; confirms session over CDP.

Can close that Chrome after. Runs relaunch as needed.

### 4. Dry run

```bash
.venv/bin/python bump.py run
```

Changes nothing. Confirm email arrives and listing titles match reality.

### 5. First real edit, one item

```bash
.venv/bin/python bump.py run --publish --limit 1
```

Open Wallapop by hand, confirm description changed.

### 6. Schedule it

```bash
cp com.enrigle.wallabump.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.plist
launchctl start com.enrigle.wallabump    # force one run now
```

That forced run is important test — where absolute-path bugs surface.

**First forced run may hang silently.** macOS asks a launchd-spawned Python for Documents folder access via a GUI dialog. Click Allow. Granted once, remembered forever.

Editing the plist later needs a reload, and a reload kills a run in progress:

```bash
launchctl unload ~/Library/LaunchAgents/com.enrigle.wallabump.plist
cp com.enrigle.wallabump.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.plist
```

### 7. Wake the Mac for it

```bash
sudo pmset repeat wakeorpoweron WS 09:55:00
pmset -g sched                            # confirm
```

`W` = Wednesday, `S` = Saturday, five minutes before launchd fires.

**Three constraints:**

- macOS allows **one** repeating power-event pair system-wide. Anything else that later runs `pmset repeat` silently replaces this.
- Scheduled wake with the **lid closed requires AC power**. On battery with the lid shut the run does not happen; launchd fires the missed job on next wake.
- The Mac must be logged in to your user session when it wakes: launchd agents and Chrome only run inside a logged-in GUI session.

Skip this step entirely if the Mac is usually awake at 10:00. launchd runs a missed job on the next wake by itself.

---

## Daily life

Two emails week. Subject line countable from lock screen:

```text
Wallabump: 12 ok, 1 skipped, 0 unverified, 0 failed
```

Body says what each listing actually got:

```text
ok          camiseta-fcb-mujer-nike-azul-roja  — added '.' → …'en las fotos.'
ok          lote-ropa-nina-5-prendas           — removed '.' → …'ver última foto'
skipped     lote-ropa-variada                  — description unsafe to toggle
```

Direction alternates run to run. Both directions bump the listing equally — an edit is an edit.

**Silence is the failure signal.** No email means run never happened — check `~/.wallapop-bump/bump.log`.

### Statuses

| Status | Meaning |
| --- | --- |
| `ok` | Edited, verified by reopening form. |
| `dry-run` | Would edit; `--publish` not passed. |
| `skipped` | Reserved, sold, or description unsafe to toggle. |
| `unverified` | Save clicked, text no change. Investigate. |
| `failed` | Edit form not found or browser error. |

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| No email at all | Run never fired. Mac asleep, or agent not loaded. | `launchctl list \| grep wallabump`, check `~/.wallapop-bump/bump.log`. |
| `RE-AUTH NEEDED` | Wallapop session in saved profile expired. | `bump.py login` again. |
| Every item `failed` | Wallapop redesigned; selectors stale. | Run `probe`, see below. Budget ~15 min. |
| Run hangs at startup, no log | macOS privacy dialog waiting offscreen. | Find the dialog, click Allow. Once only. |
| `Browser connection lost mid-run` | Chrome died or Mac slept. | Re-run. `caffeinate` should prevent the sleep case. |
| Many `unverified` | Save button selector matches wrong control. | Check `SAVE_CONTROL` against a `probe` dump. |

### When selectors break

```bash
.venv/bin/python bump.py probe
```

Attaches to running Chrome, dumps `~/.wallapop-bump/probe.html`, reports how many `/item/` links it sees. Fix constants at top of `bump.py` (`CATALOG_URL`, `EDIT_URL`, `EDIT_CONTROL`, `SAVE_CONTROL`) against dump. Expect once or twice year.

---

## What it edits

Trailing period, toggled on and off. Nothing else — never title, price, photos, category.

Descriptions it refuses to touch, to avoid publishing visible garbage:

- empty or whitespace-only
- ending in `..` or `...` — stripping one dot reads as typo
- ending in other punctuation (`?`, `!`, `,`, `:`) — `"Te interesa?."` wrong
- already at field `maxlength`

To switch to word pair instead (e.g. two alternating closing sentences), change `MARKER` at top of `bump.py`. Nothing else moves — `toggle()` and `describe()` both read it.

---

## Development

```bash
.venv/bin/python -m pytest -x          # toggle() and describe() edge cases
.venv/bin/ruff format . && .venv/bin/ruff check . --fix
.venv/bin/mypy bump.py test_bump.py --strict
```

`toggle()` and `describe()` are the only pure functions and the only things tested. Rest is browser I/O, verified by dry run.

### Layout

| File | Holds |
| --- | --- |
| `bump.py` | Everything. Config constants at top, pure logic, then browser I/O. |
| `test_bump.py` | Edge cases for the two pure functions. |
| `com.enrigle.wallabump.plist` | launchd schedule. Copy to `~/Library/LaunchAgents/`. |
| `~/.wallapop-bump/` | Chrome profile, log, probe dump. Never in the repo. |
