"""Wallabump — resurface Wallapop listings by toggling one character.

Editing a listing's text resurfaces it in Wallapop's feeds. This script performs
the smallest possible edit (toggling a trailing period) on every active listing,
verifies each save, and emails a receipt.

Wallapop's bot protection re-triggers a login screen on any *Playwright-launched*
Chrome, even with a valid session cookie. So this script never launches Chrome
through Playwright: it starts a plain, non-automated Chrome on its own profile
(or reuses one already running) and attaches to it over the Chrome DevTools
Protocol (CDP) — remote-controlling a normal browser instead of spawning a
suspicious one.

Usage:
    python bump.py login              # one-time: opens Chrome, you sign in, leave it running
    python bump.py run                # dry run (default): changes nothing
    python bump.py run --publish      # the real thing
    python bump.py probe              # dump the catalog page for selector debugging

If that Chrome is closed or the Mac reboots, the next run relaunches it from
the saved profile. If Wallapop then asks for a login anyway, you get a
RE-AUTH NEEDED email: run `python bump.py login` again.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import smtplib
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from logging.handlers import RotatingFileHandler
from pathlib import Path

from playwright.sync_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

# --- Configuration ------------------------------------------------------------

HOME_DIR = Path.home() / ".wallapop-bump"
PROFILE_DIR = HOME_DIR / "chrome-profile"
LOG_FILE = HOME_DIR / "bump.log"
PROBE_FILE = HOME_DIR / "probe.html"

KEYCHAIN_SERVICE = "wallabump-gmail"

CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

SITE = "https://es.wallapop.com"
CATALOG_URL = f"{SITE}/app/catalog/published"
EDIT_URL = f"{SITE}/app/catalog/edit/{{item_id}}"

# The edit that resurfaces the listing. Swap for a word pair (e.g. two closing
# sentences) if a trailing period ever proves unsuitable; nothing else changes.
MARKER = "."

# Only append the marker after a character that reads naturally before a period.
# Anything else ("Te interesa?", "Talla 42,") would publish visible garbage.
APPENDABLE_TAIL = re.compile(r"[\w)\]\"'»]$", re.UNICODE)

EDIT_CONTROL = re.compile(r"editar|edit", re.IGNORECASE)
SAVE_CONTROL = re.compile(r"guardar|actualizar|save|publicar", re.IGNORECASE)

# Human-ish gap between listings. A whole catalog edited in 30s is a bot.
PACE_SECONDS = (20.0, 90.0)

log = logging.getLogger("wallabump")


# --- Pure logic (the only thing under test) -----------------------------------


def toggle(desc: str | None, maxlen: int) -> str | None:
    """Return the description with its trailing marker flipped.

    Returns None when the listing must be skipped rather than mangled.
    `maxlen` of 0 or less means the limit is unknown and is not enforced.
    """
    if desc is None or not desc.strip():
        return None

    text = desc.rstrip()

    # "Casi nuevo..." would become "Casi nuevo..", which reads as a typo.
    if text.endswith(MARKER * 2):
        return None

    if text.endswith(MARKER):
        return text[: -len(MARKER)].rstrip() or None

    if not APPENDABLE_TAIL.search(text):
        return None

    if maxlen > 0 and len(text) + len(MARKER) > maxlen:
        return None

    return text + MARKER


# --- Results ------------------------------------------------------------------


@dataclass
class Item:
    href: str
    title: str
    reserved: bool
    sold: bool

    @property
    def item_id(self) -> str:
        match = re.search(r"/item/([^/?#]+)", self.href)
        return match.group(1) if match else ""


@dataclass
class Result:
    title: str
    status: str  # ok | dry-run | skipped | unverified | failed
    reason: str = ""


@dataclass
class RunReport:
    results: list[Result] = field(default_factory=list)
    fatal: str = ""

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def subject(self) -> str:
        if self.fatal:
            return f"Wallabump: FAILED — {self.fatal}"
        ok = self.count("ok") + self.count("dry-run")
        return (
            f"Wallabump: {ok} ok, "
            f"{self.count('skipped')} skipped, "
            f"{self.count('unverified')} unverified, "
            f"{self.count('failed')} failed"
        )

    @property
    def body(self) -> str:
        lines = [self.fatal] if self.fatal else []
        lines += [
            f"{r.status:<11} {r.title}" + (f"  — {r.reason}" if r.reason else "")
            for r in self.results
        ]
        if not lines:
            lines.append("No listings found.")
        lines += ["", f"Log: {LOG_FILE}"]
        return "\n".join(lines)

    @property
    def exit_code(self) -> int:
        if self.fatal:
            return 2
        return 1 if self.count("failed") or self.count("unverified") else 0


# --- Keychain + email ---------------------------------------------------------


def _security(*args: str) -> str | None:
    proc = subprocess.run(
        ["/usr/bin/security", *args], capture_output=True, text=True, check=False
    )
    return proc.stdout if proc.returncode == 0 else None


def gmail_credentials() -> tuple[str, str] | None:
    """Return (address, app_password) from the Keychain, or None if absent.

    The Keychain item stores the Gmail address as its account, so one entry
    carries both halves:
        security add-generic-password -a you@gmail.com -s wallabump-gmail -w
    """
    password = _security("find-generic-password", "-s", KEYCHAIN_SERVICE, "-w")
    metadata = _security("find-generic-password", "-s", KEYCHAIN_SERVICE)
    if password is None or metadata is None:
        return None
    account = re.search(r'"acct"<blob>="([^"]+)"', metadata)
    if account is None:
        return None
    return account.group(1), password.strip()


def send_email(report: RunReport) -> None:
    """Email the receipt. Never raises — a dead SMTP must not hide the run."""
    credentials = gmail_credentials()
    if credentials is None:
        log.error(
            "No Keychain item '%s'; cannot email. Create it with: "
            "security add-generic-password -a you@gmail.com -s %s -w",
            KEYCHAIN_SERVICE,
            KEYCHAIN_SERVICE,
        )
        return

    address, password = credentials
    message = EmailMessage()
    message["Subject"] = report.subject
    message["From"] = address
    message["To"] = address
    message.set_content(report.body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(address, password)
            smtp.send_message(message)
        log.info("Emailed report to %s", address)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("Email failed: %s", exc)


# --- Browser ------------------------------------------------------------------

_COLLECT_JS = """
() => {
  const seen = new Set();
  const items = [];
  for (const a of document.querySelectorAll('a[href*="/item/"]')) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const card = a.closest('article, li, [class*="card" i]') || a;
    const text = (card.innerText || '').toLowerCase();
    items.push({
      href: a.href,
      title: ((a.innerText || a.getAttribute('title') || '').trim().split('\\n')[0]
              || a.href),
      reserved: text.includes('reservado') || text.includes('reserved'),
      sold: text.includes('vendido') || text.includes('sold'),
    });
  }
  return items;
}
"""


def ensure_dirs() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_DIR, 0o700)


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def find_textarea(page: Page, timeout: float = 8000) -> Locator | None:
    """The description field. A Wallapop edit form has exactly one textarea."""
    textarea = page.locator("textarea").first
    try:
        textarea.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeout:
        return None
    return textarea


def is_logged_in(page: Page) -> bool:
    if "login" in page.url or "auth" in page.url:
        return False
    try:
        page.wait_for_selector('a[href*="/item/"]', timeout=15000)
    except PlaywrightTimeout:
        return False
    return True


def open_edit_form(page: Page, item: Item) -> Locator | None:
    """Open the item's edit form. Tries the direct URL, falls back to clicking."""
    if item.item_id:
        page.goto(EDIT_URL.format(item_id=item.item_id), wait_until="domcontentloaded")
        textarea = find_textarea(page)
        if textarea is not None:
            return textarea
        log.debug(
            "Direct edit URL gave no form for %s; using the item page", item.title
        )

    page.goto(item.href, wait_until="domcontentloaded")
    control = page.get_by_role("link", name=EDIT_CONTROL).or_(
        page.get_by_role("button", name=EDIT_CONTROL)
    )
    control.first.click(timeout=8000)
    return find_textarea(page)


def read_description(page: Page, item: Item) -> tuple[str, int] | None:
    textarea = open_edit_form(page, item)
    if textarea is None:
        return None
    raw_maxlen = textarea.get_attribute("maxlength")
    maxlen = int(raw_maxlen) if raw_maxlen and raw_maxlen.isdigit() else 0
    return textarea.input_value(), maxlen


def bump_item(page: Page, item: Item, publish: bool) -> Result:
    current = read_description(page, item)
    if current is None:
        return Result(item.title, "failed", "edit form not found")

    description, maxlen = current
    updated = toggle(description, maxlen)
    if updated is None:
        return Result(item.title, "skipped", "description unsafe to toggle")

    if not publish:
        return Result(item.title, "dry-run", f"would set …{updated[-20:]!r}")

    textarea = find_textarea(page)
    if textarea is None:
        return Result(item.title, "failed", "edit form vanished before fill")
    textarea.fill(updated)
    page.get_by_role("button", name=SAVE_CONTROL).first.click(timeout=8000)
    page.wait_for_timeout(4000)

    # Verify by reopening the form: same selector, so no second guess to be wrong.
    saved = read_description(page, item)
    if saved is None:
        return Result(item.title, "unverified", "could not reopen form to verify")
    if saved[0].rstrip() != updated:
        return Result(item.title, "unverified", "description unchanged after save")
    return Result(item.title, "ok")


def cdp_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def launch_chrome() -> None:
    """Start a real, non-automated Chrome with its debugging port open.

    No `--enable-automation`, no Playwright involved in the launch — Chrome
    never sees itself as automated, so none of Wallapop's re-login triggers
    fire. The profile dir keeps the session cookies, so relaunching after a
    reboot or an accidental Cmd+Q is the same as a human reopening Chrome.
    Output goes to /dev/null: Chrome's own log lines must not land in the
    terminal that launched it.
    """
    ensure_dirs()
    subprocess.Popen(
        [
            CHROME_BINARY,
            f"--user-data-dir={PROFILE_DIR}",
            f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run",
            "--no-default-browser-check",
            CATALOG_URL,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_chrome(wait_seconds: float = 15.0) -> None:
    """Launch Chrome if the debugging port is dead, then wait for it."""
    if cdp_reachable():
        return
    log.info("Chrome not running; launching it from the saved profile.")
    launch_chrome()
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if cdp_reachable():
            time.sleep(2)  # let the first tab settle before attaching
            return
        time.sleep(0.5)
    raise RuntimeError(
        f"Launched Chrome but {CDP_URL} never came up within {wait_seconds:.0f}s. "
        "Run: python bump.py login"
    )


def attach(playwright: Playwright) -> Browser:
    ensure_chrome()
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=5000)
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Chrome with remote debugging isn't reachable at {CDP_URL}. "
            "Run: python bump.py login"
        ) from exc


def run(publish: bool, limit: int) -> RunReport:
    report = RunReport()
    ensure_dirs()

    # A full catalog takes ~30 min at human pace; idle sleep mid-run kills the
    # CDP connection. caffeinate -w exits by itself when this process does.
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])

    with sync_playwright() as playwright:
        browser = attach(playwright)
        context = browser.contexts[0]
        page = context.new_page()
        try:
            page.goto(CATALOG_URL, wait_until="domcontentloaded")

            if not is_logged_in(page):
                report.fatal = "RE-AUTH NEEDED — run: python bump.py login"
                log.error(report.fatal)
                return report

            raw = page.evaluate(_COLLECT_JS)
            items = [Item(**entry) for entry in raw]
            log.info("Found %d listings", len(items))

            active = [i for i in items if not i.reserved and not i.sold]
            for item in items:
                if item.reserved or item.sold:
                    state = "reserved" if item.reserved else "sold"
                    report.results.append(Result(item.title, "skipped", state))

            if limit > 0:
                active = active[:limit]

            for index, item in enumerate(active):
                try:
                    result = bump_item(page, item, publish)
                except (PlaywrightError, PlaywrightTimeout, ValueError) as exc:
                    result = Result(item.title, "failed", type(exc).__name__)
                log.info("%s: %s %s", item.title, result.status, result.reason)
                report.results.append(result)

                if page.is_closed() or not browser.is_connected():
                    report.fatal = "Browser connection lost mid-run (Mac asleep?)"
                    log.error(report.fatal)
                    return report

                if index < len(active) - 1:
                    time.sleep(random.uniform(*PACE_SECONDS))
        finally:
            try:
                page.close()  # only our tab — the user's Chrome keeps running
            except PlaywrightError:
                pass  # connection already gone; nothing left to close

    return report


def login() -> int:
    """Get a signed-in, remote-debuggable Chrome running, then verify it."""
    if cdp_reachable():
        log.info("Chrome with remote debugging is already running.")
    else:
        log.info("Opening Chrome — sign in, then come back here.")
        launch_chrome()

    input("Press Enter once your listings are visible… ")

    if PROFILE_DIR.exists():
        os.chmod(PROFILE_DIR, 0o700)

    with sync_playwright() as playwright:
        browser = attach(playwright)
        page = browser.contexts[0].new_page()
        page.goto(CATALOG_URL, wait_until="domcontentloaded")
        logged_in = is_logged_in(page)
        page.close()

    if logged_in:
        log.info("Session confirmed. Leave this Chrome window open and running —")
        log.info("do not Cmd+Q it or restart the Mac between scheduled runs.")
    else:
        log.info("NOT logged in yet — finish signing in, then run this again.")
    return 0 if logged_in else 2


def probe() -> int:
    """Dump the catalog page so selectors can be fixed when Wallapop changes."""
    ensure_dirs()
    with sync_playwright() as playwright:
        browser = attach(playwright)
        page = browser.contexts[0].new_page()
        try:
            page.goto(CATALOG_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            PROBE_FILE.write_text(page.content(), encoding="utf-8")
            log.info("URL: %s", page.url)
            log.info(
                "Anchors matching /item/: %d",
                page.locator('a[href*="/item/"]').count(),
            )
            log.info("Wrote %s", PROBE_FILE)
        finally:
            page.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["run", "login", "probe"])
    parser.add_argument(
        "--publish",
        action="store_true",
        help="actually save changes (default is a dry run)",
    )
    parser.add_argument("--limit", type=int, default=0, help="only touch N listings")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.command == "login":
        return login()
    if args.command == "probe":
        return probe()

    log.info("Starting run (publish=%s, limit=%s)", args.publish, args.limit or "all")
    try:
        report = run(args.publish, args.limit)
    except (RuntimeError, PlaywrightError, PlaywrightTimeout, OSError) as exc:
        report = RunReport(fatal=str(exc))
        log.exception("Run aborted")

    send_email(report)
    log.info("Done — %s", report.subject)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
