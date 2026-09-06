# Wallabump

Twice a week (Wednesday and Saturday, 10:00), toggle one character in every active Wallapop listing so the items
resurface in the feeds. Emails a receipt after every run.

No LLM, no API key, no cloud. One dependency (`playwright`), driving your real
Chrome from your own machine at human pace.

> Wallapop's Terms of Service prohibit automated access. This drives your own
> account against your own listings and is the lowest-signature approach
> available, but the residual risk of account action is real. Your call.

### Why it drives a separate Chrome

Wallapop's bot protection forces a re-login screen on any *Playwright-launched*
Chrome — even one holding a valid session cookie in its profile directory.
Both headless and headed Playwright launches hit this.

The workaround: the script starts a plain, non-automated Chrome on its own
profile (`~/.wallapop-bump/chrome-profile/`), or reuses one already running,
and attaches to it over Chrome's DevTools Protocol (CDP). A plain launch looks
like a human reopening Chrome, so the re-login trigger does not fire.

**This means:** quitting that Chrome or rebooting the Mac is fine. The next
run relaunches it from the saved profile. If Wallapop asks for a login anyway,
the run emails `RE-AUTH NEEDED`; run `bump.py login` again.

**Security note:** the debugging port (`127.0.0.1:9222`) only accepts local
connections, but any other process on this Mac could in principle attach to
it too while Chrome is running with it open. Acceptable on a personal machine,
worth knowing.

---

## Setup

### 1. Environment

```bash
cd /Users/enrigle/Documents/code/wallapop
/opt/homebrew/bin/python3.14 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The system default `python3` on this Mac is anaconda 3.8.5 — too old. Use the
Homebrew 3.14 interpreter above.

No `playwright install` needed: the script attaches to the Google Chrome
already in `/Applications`, not a downloaded Chromium.

### 2. Gmail credentials in the Keychain

One entry holds both the address and the app password:

```bash
security add-generic-password -a you@gmail.com -s wallabump-gmail -w
```

It prompts for the password. Use a Gmail **app password**
(<https://myaccount.google.com/apppasswords>), not your account password.
Reports are sent from that address to itself.

### 3. Sign in once

```bash
.venv/bin/python bump.py login
```

Opens a real Chrome (not automated) on the profile at
`~/.wallapop-bump/chrome-profile/`, mode 700. Sign in normally — Google
account, SMS/email code, whatever it asks. Once your listings are visible,
come back to the terminal and press Enter; it confirms the session over CDP.

You can close that Chrome afterwards. Runs relaunch it as needed.

### 4. Dry run

```bash
.venv/bin/python bump.py run
```

Changes nothing. Confirm the email arrives and that the listing titles match
reality.

### 5. First real edit, one item

```bash
.venv/bin/python bump.py run --publish --limit 1
```

Open Wallapop by hand and confirm the description changed.

### 6. Schedule it

```bash
cp com.enrigle.wallabump.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.enrigle.wallabump.plist
launchctl start com.enrigle.wallabump    # force one run now
```

That forced run is the important test — it is where absolute-path bugs surface.

### 7. Wake the Mac for it

```bash
sudo pmset repeat wakeorpoweron WS 09:55:00
pmset -g sched                            # confirm
```

`W` = Wednesday, `S` = Saturday, five minutes before launchd fires.

**Three constraints:**

- macOS allows **one** repeating power-event pair system-wide. Anything else that
  later runs `pmset repeat` silently replaces this.
- Scheduled wake with the **lid closed requires AC power**. On battery with the
  lid shut the run does not happen; launchd fires the missed job on next wake.
- The Mac must be logged in to your user session when it wakes: launchd
  agents and Chrome only run inside a logged-in GUI session.

---

## Daily life

Two emails a week. Subject line is countable from the lock screen:

```
Wallabump: 12 ok, 1 skipped, 0 unverified, 0 failed
```

**Silence is the failure signal.** No email means the run never happened —
check `~/.wallapop-bump/bump.log`.

### Statuses

| Status | Meaning |
|---|---|
| `ok` | Edited and verified by reopening the form. |
| `dry-run` | Would have edited; `--publish` was not passed. |
| `skipped` | Reserved, sold, or a description unsafe to toggle. |
| `unverified` | Save clicked, but the text did not change. Investigate. |
| `failed` | Edit form not found or a browser error. |

### When you get `RE-AUTH NEEDED`

The Wallapop session in the saved profile expired. Run `bump.py login`
again — it reuses the running Chrome if the debugging port is reachable, or
opens a fresh one otherwise.

### When selectors break

Wallapop redesigns; the email shows every item as `failed`. Then:

```bash
.venv/bin/python bump.py probe
```

Attaches to the running Chrome, dumps `~/.wallapop-bump/probe.html`, and
reports how many `/item/` links it can see. Fix the constants at the top of
`bump.py` (`CATALOG_URL`, `EDIT_URL`, `EDIT_CONTROL`, `SAVE_CONTROL`) against
that dump. Budget ~15 minutes. Expect this once or twice a year.

---

## What it edits

A trailing period, toggled on and off. Nothing else — never the title, price,
photos, or category.

Descriptions it refuses to touch, to avoid publishing visible garbage:

- empty or whitespace-only
- ending in `..` or `...` — stripping one dot reads as a typo
- ending in other punctuation (`?`, `!`, `,`, `:`) — `"Te interesa?."` is wrong
- already at the field's `maxlength`

To switch to a word pair instead (e.g. two alternating closing sentences),
change `MARKER` at the top of `bump.py`. Nothing else needs to move.

There is no state file. Each run reads the live description and decides from
what is actually there, so a half-finished run self-heals on the next one.

---

## Development

```bash
.venv/bin/python -m pytest -x          # toggle() edge cases
.venv/bin/ruff format . && .venv/bin/ruff check . --fix
.venv/bin/mypy bump.py test_bump.py --strict
```

`toggle()` is the only pure function and the only thing tested. Everything else
is browser I/O, verified by the dry run.
