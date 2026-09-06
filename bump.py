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

Sites live in sites.toml, one section each. Everything site-specific — catalog
URL, edit URL, link shape, button words — is read from there, so adding a second
marketplace is a config entry, not a code change.

Usage:
    python bump.py login              # one-time: opens Chrome, you sign in, leave it running
    python bump.py run                # dry run (default): changes nothing
    python bump.py run --publish      # the real thing
    python bump.py run --site vinted  # one site instead of all of them
    python bump.py probe --site vinted  # dump a catalog page for selector debugging

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

import tomllib
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
SITES_FILE = Path(__file__).resolve().parent / "sites.toml"

KEYCHAIN_SERVICE = "wallabump-gmail"

CHROME_BINARY = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

# The edit that resurfaces the listing. Swap for a word pair (e.g. two closing
# sentences) if a trailing period ever proves unsuitable; nothing else changes.
MARKER = "."

# Only append the marker after a character that reads naturally before a period.
# Anything else ("Te interesa?", "Talla 42,") would publish visible garbage.
APPENDABLE_TAIL = re.compile(r"[\w)\]\"'»]$", re.UNICODE)

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


def describe(updated: str) -> str:
    """Say which way toggle() went and what the text now ends with.

    toggle() never returns a removal result ending in MARKER, and refuses the
    doubled-marker case outright, so endswith is an exact test of the direction.
    """
    direction = "added" if updated.endswith(MARKER) else "removed"
    return f"{direction} {MARKER!r} → …{updated[-20:]!r}"


# --- Site configuration -------------------------------------------------------

SITE_KEYS = (
    "catalog_url",
    "edit_url",
    "link_pattern",
    "id_regex",
    "card_selector",
    "description_selector",
    "edit_control",
    "save_control",
    "reserved",
    "sold",
)


@dataclass(frozen=True)
class Site:
    """One marketplace, as described by its section of sites.toml."""

    name: str
    catalog_url: str
    edit_url: str
    link_pattern: str
    id_regex: str
    card_selector: str
    description_selector: str
    edit_control: str
    save_control: str
    reserved: tuple[str, ...]
    sold: tuple[str, ...]

    def item_id(self, href: str) -> str:
        """The listing id inside a listing URL, or "" when it does not match."""
        match = re.search(self.id_regex, href)
        return match.group(1) if match else ""

    def edit_link(self, href: str) -> str:
        return self.edit_url.format(item_id=self.item_id(href))

    @property
    def link_selector(self) -> str:
        return f'a[href*="{self.link_pattern}"]'

    @property
    def edit_pattern(self) -> re.Pattern[str]:
        return re.compile(self.edit_control, re.IGNORECASE)

    @property
    def save_pattern(self) -> re.Pattern[str]:
        return re.compile(self.save_control, re.IGNORECASE)


def load_sites(path: Path = SITES_FILE) -> dict[str, Site]:
    """Read sites.toml. A missing key must fail loudly, not skip a wardrobe."""
    if not path.exists():
        raise RuntimeError(f"No site config at {path}")

    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    sites: dict[str, Site] = {}
    for name, entry in raw.items():
        missing = [key for key in SITE_KEYS if key not in entry]
        if missing:
            raise RuntimeError(
                f"Site '{name}' in {path} is missing: {', '.join(missing)}"
            )
        sites[name] = Site(
            name=name,
            catalog_url=entry["catalog_url"],
            edit_url=entry["edit_url"],
            link_pattern=entry["link_pattern"],
            id_regex=entry["id_regex"],
            card_selector=entry["card_selector"],
            description_selector=entry["description_selector"],
            edit_control=entry["edit_control"],
            save_control=entry["save_control"],
            reserved=tuple(entry["reserved"]),
            sold=tuple(entry["sold"]),
        )

    if not sites:
        raise RuntimeError(f"No sites configured in {path}")
    return sites


def probe_file(site: Site) -> Path:
    return HOME_DIR / f"probe-{site.name}.html"


# --- Results ------------------------------------------------------------------


@dataclass
class Item:
    href: str
    title: str
    reserved: bool
    sold: bool


@dataclass
class Result:
    site: str
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
        subject = (
            f"Wallabump: {ok} ok, "
            f"{self.count('skipped')} skipped, "
            f"{self.count('unverified')} unverified, "
            f"{self.count('failed')} failed"
        )
        # Keep the re-auth signal readable from the lock screen even when only
        # one of several sites has lost its session.
        if any("RE-AUTH" in r.reason for r in self.results):
            subject += " — RE-AUTH NEEDED"
        return subject

    @property
    def body(self) -> str:
        lines = [self.fatal] if self.fatal else []
        lines += [
            f"{r.site:<9} {r.status:<11} {r.title}"
            + (f"  — {r.reason}" if r.reason else "")
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

# Takes its site config as an argument rather than being built by string
# interpolation, so no config value is ever spliced into JavaScript source.
_COLLECT_JS = """
(cfg) => {
  const seen = new Set();
  const items = [];
  for (const a of document.querySelectorAll(cfg.selector)) {
    if (seen.has(a.href)) continue;
    seen.add(a.href);
    const card = a.closest(cfg.card) || a;
    const text = (card.innerText || '').toLowerCase();
    items.push({
      href: a.href,
      title: ((a.innerText || a.getAttribute('title') || '').trim().split('\\n')[0]
              || a.href),
      reserved: cfg.reserved.some(word => text.includes(word)),
      sold: cfg.sold.some(word => text.includes(word)),
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


def find_textarea(page: Page, site: Site, timeout: float = 8000) -> Locator | None:
    """The description field.

    Wallapop's edit form holds one textarea; Vinted's holds two, the second
    being an unrelated single-character field. Taking `.first` blindly would be
    right by luck on one site and a coin flip on the next, so each site names
    its own selector in sites.toml.
    """
    textarea = page.locator(site.description_selector).first
    try:
        textarea.wait_for(state="visible", timeout=timeout)
    except PlaywrightTimeout:
        return None
    return textarea


def is_logged_in(page: Page, site: Site) -> bool:
    if "login" in page.url or "auth" in page.url:
        return False
    try:
        page.wait_for_selector(site.link_selector, timeout=15000)
    except PlaywrightTimeout:
        return False
    return True


def open_edit_form(page: Page, site: Site, item: Item) -> Locator | None:
    """Open the item's edit form. Tries the direct URL, falls back to clicking."""
    if site.item_id(item.href):
        page.goto(site.edit_link(item.href), wait_until="domcontentloaded")
        textarea = find_textarea(page, site)
        if textarea is not None:
            return textarea
        log.debug(
            "Direct edit URL gave no form for %s; using the item page", item.title
        )

    page.goto(item.href, wait_until="domcontentloaded")
    control = page.get_by_role("link", name=site.edit_pattern).or_(
        page.get_by_role("button", name=site.edit_pattern)
    )
    control.first.click(timeout=8000)
    return find_textarea(page, site)


def read_description(page: Page, site: Site, item: Item) -> tuple[str, int] | None:
    textarea = open_edit_form(page, site, item)
    if textarea is None:
        return None
    raw_maxlen = textarea.get_attribute("maxlength")
    maxlen = int(raw_maxlen) if raw_maxlen and raw_maxlen.isdigit() else 0
    return textarea.input_value(), maxlen


def bump_item(page: Page, site: Site, item: Item, publish: bool) -> Result:
    current = read_description(page, site, item)
    if current is None:
        return Result(site.name, item.title, "failed", "edit form not found")

    description, maxlen = current
    updated = toggle(description, maxlen)
    if updated is None:
        return Result(site.name, item.title, "skipped", "description unsafe to toggle")

    if not publish:
        return Result(
            site.name, item.title, "dry-run", f"would have {describe(updated)}"
        )

    textarea = find_textarea(page, site)
    if textarea is None:
        return Result(site.name, item.title, "failed", "edit form vanished before fill")
    textarea.fill(updated)
    page.get_by_role("button", name=site.save_pattern).first.click(timeout=8000)
    page.wait_for_timeout(4000)

    # Verify by reopening the form: same selector, so no second guess to be wrong.
    saved = read_description(page, site, item)
    if saved is None:
        return Result(
            site.name, item.title, "unverified", "could not reopen form to verify"
        )
    if saved[0].rstrip() != updated:
        return Result(
            site.name, item.title, "unverified", "description unchanged after save"
        )
    return Result(site.name, item.title, "ok", describe(updated))


def cdp_reachable() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1.5):
            return True
    except OSError:
        return False


def launch_chrome(start_url: str) -> None:
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
            start_url,
        ],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def ensure_chrome(start_url: str, wait_seconds: float = 15.0) -> None:
    """Launch Chrome if the debugging port is dead, then wait for it."""
    if cdp_reachable():
        return
    log.info("Chrome not running; launching it from the saved profile.")
    launch_chrome(start_url)
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


def attach(playwright: Playwright, start_url: str) -> Browser:
    ensure_chrome(start_url)
    try:
        return playwright.chromium.connect_over_cdp(CDP_URL, timeout=5000)
    except PlaywrightError as exc:
        raise RuntimeError(
            f"Chrome with remote debugging isn't reachable at {CDP_URL}. "
            "Run: python bump.py login"
        ) from exc


def load_whole_page(page: Page, site: Site, rounds: int = 15) -> None:
    """Scroll until the listing count stops growing.

    Vinted's wardrobe is a lazy grid: cards below the fold are not rendered, so
    they report no text at all and every reserved or sold badge reads as absent.
    Scrolling first is what makes those badges visible.
    """
    seen = -1
    for _ in range(rounds):
        count = page.locator(site.link_selector).count()
        if count == seen:
            break
        seen = count
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(800)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(1000)


def collect(page: Page, site: Site) -> list[Item]:
    load_whole_page(page, site)
    raw = page.evaluate(
        _COLLECT_JS,
        {
            "selector": site.link_selector,
            "card": site.card_selector,
            "reserved": list(site.reserved),
            "sold": list(site.sold),
        },
    )
    items = [Item(**entry) for entry in raw]
    # A link matching the pattern is not always a listing: Vinted's wardrobe
    # also carries /items/new and a favourites link. Anything without an id is
    # not something we can open an edit form for.
    return [item for item in items if site.item_id(item.href)]


def bump_site(
    page: Page, browser: Browser, site: Site, publish: bool, limit: int
) -> tuple[list[Result], str]:
    """Bump one site. Returns its results and a fatal message, "" when fine.

    A site that cannot be read is that site's problem: it reports a failure and
    the caller moves on to the next one. Only a dead browser is fatal, because
    then no site can be reached.
    """
    results: list[Result] = []
    page.goto(site.catalog_url, wait_until="domcontentloaded")

    if not is_logged_in(page, site):
        reason = "RE-AUTH NEEDED — run: python bump.py login"
        log.error("%s: %s", site.name, reason)
        return [Result(site.name, site.catalog_url, "failed", reason)], ""

    items = collect(page, site)
    log.info("%s: found %d listings", site.name, len(items))

    for item in items:
        if item.reserved or item.sold:
            state = "reserved" if item.reserved else "sold"
            results.append(Result(site.name, item.title, "skipped", state))

    active = [i for i in items if not i.reserved and not i.sold]
    if limit > 0:
        active = active[:limit]

    for index, item in enumerate(active):
        try:
            result = bump_item(page, site, item, publish)
        except (PlaywrightError, PlaywrightTimeout, ValueError) as exc:
            result = Result(site.name, item.title, "failed", type(exc).__name__)
        log.info("%s: %s %s", item.title, result.status, result.reason)
        results.append(result)

        if page.is_closed() or not browser.is_connected():
            return results, "Browser connection lost mid-run (Mac asleep?)"

        if index < len(active) - 1:
            time.sleep(random.uniform(*PACE_SECONDS))

    return results, ""


def run(sites: list[Site], publish: bool, limit: int) -> RunReport:
    report = RunReport()
    ensure_dirs()

    # A full catalog takes ~30 min at human pace; idle sleep mid-run kills the
    # CDP connection. caffeinate -w exits by itself when this process does.
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])

    with sync_playwright() as playwright:
        browser = attach(playwright, sites[0].catalog_url)
        context = browser.contexts[0]
        page = context.new_page()
        try:
            for site in sites:
                results, fatal = bump_site(page, browser, site, publish, limit)
                report.results.extend(results)
                if fatal:
                    report.fatal = fatal
                    log.error(fatal)
                    return report
        finally:
            try:
                page.close()  # only our tab — the user's Chrome keeps running
            except PlaywrightError:
                pass  # connection already gone; nothing left to close

    return report


def login(sites: list[Site]) -> int:
    """Get a signed-in, remote-debuggable Chrome running, then verify each site.

    One profile carries every site's session, so this checks them all and names
    the ones still needing a sign-in.
    """
    if cdp_reachable():
        log.info("Chrome with remote debugging is already running.")
    else:
        log.info("Opening Chrome — sign in, then come back here.")
        launch_chrome(sites[0].catalog_url)

    names = ", ".join(site.name for site in sites)
    input(f"Press Enter once you are signed in to {names}… ")

    if PROFILE_DIR.exists():
        os.chmod(PROFILE_DIR, 0o700)

    missing: list[str] = []
    with sync_playwright() as playwright:
        browser = attach(playwright, sites[0].catalog_url)
        page = browser.contexts[0].new_page()
        try:
            for site in sites:
                page.goto(site.catalog_url, wait_until="domcontentloaded")
                if is_logged_in(page, site):
                    log.info("%s: session confirmed.", site.name)
                else:
                    log.info("%s: NOT logged in.", site.name)
                    missing.append(site.name)
        finally:
            page.close()

    if missing:
        log.info("Finish signing in to %s, then run this again.", ", ".join(missing))
        return 2

    log.info("All sessions confirmed. Chrome may be closed; runs relaunch it.")
    return 0


def probe(sites: list[Site]) -> int:
    """Dump each catalog page so selectors can be fixed after a redesign."""
    ensure_dirs()
    with sync_playwright() as playwright:
        browser = attach(playwright, sites[0].catalog_url)
        page = browser.contexts[0].new_page()
        try:
            for site in sites:
                page.goto(site.catalog_url, wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
                target = probe_file(site)
                target.write_text(page.content(), encoding="utf-8")
                log.info("%s: URL %s", site.name, page.url)
                log.info(
                    "%s: anchors matching %s: %d",
                    site.name,
                    site.link_pattern,
                    page.locator(site.link_selector).count(),
                )
                log.info("%s: wrote %s", site.name, target)
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
    parser.add_argument(
        "--limit", type=int, default=0, help="only touch N listings per site"
    )
    parser.add_argument(
        "--site", default="all", help="a site from sites.toml, or 'all' (default)"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    try:
        configured = load_sites()
        if args.site == "all":
            chosen = list(configured.values())
        elif args.site in configured:
            chosen = [configured[args.site]]
        else:
            known = ", ".join(sorted(configured))
            raise RuntimeError(f"Unknown site '{args.site}'. Configured: {known}")
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2

    if args.command == "login":
        return login(chosen)
    if args.command == "probe":
        return probe(chosen)

    log.info(
        "Starting run (sites=%s, publish=%s, limit=%s)",
        ", ".join(site.name for site in chosen),
        args.publish,
        args.limit or "all",
    )
    try:
        report = run(chosen, args.publish, args.limit)
    except (RuntimeError, PlaywrightError, PlaywrightTimeout, OSError) as exc:
        report = RunReport(fatal=str(exc))
        log.exception("Run aborted")

    send_email(report)
    log.info("Done — %s", report.subject)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
