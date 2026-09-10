# Wallabump

Twice week (Thu + Sun, 21:55), toggle one char in every active listing so items resurface in feeds. Emails receipt after every run.

Two sites configured: **Wallapop** and **Vinted**. Sites are config, not code — `sites.toml` holds one section per marketplace, so a third is a config entry.

No LLM, no API key, no cloud. One dependency (`playwright`), driving your real Chrome from your machine at human pace.

> Wallapop's Terms of Service prohibit automated access. This drives your own account against your own listings and is the lowest-signature approach available, but the residual risk of account action is real. Your call.

## Contents

- [Commands](#commands) — cheat sheet
- [How it works](#how-it-works) — the run, start to finish
- [Why a separate Chrome](#why-a-separate-chrome) — the one non-obvious design choice
- [Setup](#setup) — eight steps, once
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
| `bump.py run --publish --limit 1` | Real, one listing per site. Smoke test. |
| `bump.py run --site vinted` | One site instead of all. |
| `bump.py probe` | Dump catalog HTML when selectors break. |
| `-v` | Debug logging on any of the above. |

`--site` takes any section name from `sites.toml`, or `all`, the default. An unknown name exits 2 and lists what is configured.

All take the venv interpreter: `.venv/bin/python bump.py …`

---

## How it works

1. **Load config.** Reads `sites.toml`. A missing key names the site and the key, and exits 2 rather than running half-configured.
2. **Attach.** Checks `127.0.0.1:9222`. Dead → launches plain Chrome on the saved profile, waits up to 15s.
3. **Stay awake.** Spawns `caffeinate -i` for the run's lifetime. A full catalog takes ~25 min per site and idle sleep kills the connection. This holds an awake Mac awake; it cannot rescue one that is asleep.
4. **Per site, in order:** collect, toggle, verify.
5. **Collect.** Scrapes the live catalogue for that site's link pattern. Reserved and sold are marked skipped.
6. **Toggle.** Per listing: open edit form, read description, flip trailing period, save.
7. **Verify.** Reopens the form and compares. Mismatch → `unverified`.
8. **Pace.** Sleeps 20 to 90s between listings, random.
9. **Report.** One email covering every site, exits 0 (clean), 1 (some failures), or 2 (fatal).

**Site quirks are config, not special cases.** Vinted's edit form holds two textareas, so its section names the description one rather than trusting `.first`. Its cards hang off a `data-testid` instead of an `<article>`, and its grid renders lazily, so a run scrolls the wardrobe before reading it. Links matching the pattern but carrying no id, like Vinted's `/items/new`, are dropped.

**A sleeping Mac aborts the run instead of limping.** After each listing the run compares the wall clock against the monotonic clock. Only a suspended process sees those two diverge, so a gap over a minute is proof the Mac slept rather than the step being slow. The run stops and says so.

**One site failing does not stop the others.** A site that will not load, or whose session expired, records its own `failed` line and the run moves to the next. Only a dead browser aborts everything.

**No state file.** Every run reads what is actually there and decides fresh. So a new listing is included automatically, a half-finished run self-heals on the next one, and a failed listing is retried like any other. Nothing tracks "which ones failed last time" because nothing needs to.

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

One entry holds both address and app password, shared by every site:

```bash
security add-generic-password -a you@gmail.com -s wallabump-gmail -w
```

Prompts for password. Use Gmail **app password** (<https://myaccount.google.com/apppasswords>), not account password. Reports sent from that address to itself.

Nothing else stores a credential. No secret ever lands in the repo.

### 3. Point Vinted at your own wardrobe

`catalog_url` under `[vinted]` in `sites.toml` contains a member id, so it is account-specific. Open Vinted, click **Armario** in the header, and copy the URL. It looks like `https://www.vinted.es/member/185114459`.

Wallapop needs no such edit — its catalogue URL is the same for every account.

### 4. Sign in once

```bash
.venv/bin/python bump.py login
```

Opens real Chrome (not automated) on profile at `~/.wallapop-bump/chrome-profile/`, mode 700. Sign in to **every** site in `sites.toml` — one profile carries all sessions. Back to terminal, press Enter; it checks each site and names any still signed out.

Can close that Chrome after. Runs relaunch as needed.

### 5. Dry run

```bash
.venv/bin/python bump.py run
```

Changes nothing. Confirm email arrives and listing titles match reality.

### 6. First real edit, one item

```bash
.venv/bin/python bump.py run --publish --limit 1
```

One listing per site. Open both by hand, confirm the descriptions changed.

### 7. Schedule it

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

### 8. Do not bother scheduling a wake

There is deliberately no `pmset` wake here, and adding one back would make things worse rather than better.

**A scheduled wake on battery is a DarkWake.** macOS gives the machine a few seconds of background CPU and then puts it straight back to sleep. A 50-minute catalogue edit cannot run in four-second slices. This was observed, not assumed: a morning run got one DarkWake every sixteen minutes and spent ninety minutes producing nothing.

The schedule is 21:55 instead, because a laptop is usually open and genuinely awake in the evening. When it is awake, `caffeinate -i` holds it through the run and no power management is involved at all.

**If the Mac is asleep at 21:55, nothing runs and nothing breaks.** launchd fires the missed job on the next wake. There is no state file, so whenever it does run it reads the live descriptions and covers everything.

The one hard requirement: the Mac must be logged in to your user session. launchd agents and Chrome only run inside a logged-in GUI session.

---

## Daily life

Two emails week. Subject line countable from lock screen:

```text
Wallabump: 12 ok, 1 skipped, 0 unverified, 0 failed
```

Body says what each listing actually got:

```text
wallapop  ok          camiseta-fcb-mujer-nike-azul-roja  — added '.' → …'en las fotos.'
wallapop  ok          lote-ropa-nina-5-prendas           — removed '.' → …'ver última foto'
vinted    skipped     jersey-lana-talla-m                — description unsafe to toggle
```

First column is the site.

Direction alternates run to run. Both directions bump the listing equally — an edit is an edit.

**Silence is the failure signal.** No email means run never happened — check `~/.wallapop-bump/bump.log`.

### A caveat on sold and reserved

Both sites are told which badge words mean "leave it alone". That was verified on Wallapop, where reserved and sold listings do appear in the catalogue. It could not be verified on Vinted, because the wardrobe held no sold or reserved listing to read the wording off. If Vinted words its badge differently, such a listing would get its period toggled. The cost is one pointless edit to something already sold, not a damaged listing.

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
| `RE-AUTH NEEDED` in subject | One site's session in the saved profile expired. Body says which. | `bump.py login` again. |
| `Unknown site 'x'` | Typo in `--site`, or the section is still commented out in `sites.toml`. | Use a name the error lists. |
| `Site 'x' is missing: …` | `sites.toml` section is incomplete. | Add the named keys. |
| Every item `failed` | Wallapop redesigned; selectors stale. | Run `probe`, see below. Budget ~15 min. |
| Run hangs at startup, no log | macOS privacy dialog waiting offscreen. | Find the dialog, click Allow. Once only. |
| `Browser connection lost mid-run` | Chrome died or Mac slept. | Re-run. `caffeinate` covers idle sleep while the Mac is awake. |
| `Mac slept for N min mid-run` | The Mac was asleep or on a DarkWake. Usually battery plus a closed lid. | Nothing to fix. The next run covers every listing. |
| `CDP handshake did not finish` | Chrome answered but the Mac was mid-wake. | Re-run while the Mac is awake. Not a login problem. |
| Many `unverified` | Save button selector matches wrong control. | Check `SAVE_CONTROL` against a `probe` dump. |

### When selectors break

```bash
.venv/bin/python bump.py probe
```

Attaches to running Chrome, dumps `~/.wallapop-bump/probe-<site>.html` per site, reports how many listing links each shows. Fix that site's section in `sites.toml` against the dump — no code change. Expect once or twice year per site.

---

## What it edits

Trailing period, toggled on and off. Nothing else — never title, price, photos, category.

Descriptions it refuses to touch, to avoid publishing visible garbage:

- empty or whitespace-only, on either site
- ending in `..` or `...` — stripping one dot reads as typo
- ending in other punctuation (`?`, `!`, `,`, `:`) — `"Te interesa?."` wrong
- already at field `maxlength`

To switch to word pair instead (e.g. two alternating closing sentences), change `MARKER` at top of `bump.py`. Nothing else moves — `toggle()` and `describe()` both read it. The marker is deliberately global: one toggle rule, tested once, applied everywhere.

---

## Development

```bash
.venv/bin/python -m pytest -x          # pure logic and config loading
.venv/bin/ruff format . && .venv/bin/ruff check . --fix
.venv/bin/mypy bump.py test_bump.py --strict
```

`toggle()`, `describe()` and the `Site` config layer are the pure parts and the only things tested. Rest is browser I/O, verified by dry run.

### Layout

| File | Holds |
| --- | --- |
| `bump.py` | Everything. Shared constants at top, pure logic, then browser I/O. |
| `sites.toml` | Per-site URLs and selectors. Edit this when a site redesigns. |
| `test_bump.py` | Edge cases for the pure functions and the config loader. |
| `com.enrigle.wallabump.plist` | launchd schedule. Copy to `~/Library/LaunchAgents/`. |
| `~/.wallapop-bump/` | Chrome profile, log, per-site probe dumps. Never in the repo. |
