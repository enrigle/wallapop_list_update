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
    python bump.py login              # one-time: opens Chrome, you sign in, it closes after
    python bump.py run                # dry run (default): changes nothing
    python bump.py run --publish      # the real thing
    python bump.py run --site vinted  # one site instead of all of them
    python bump.py probe --site vinted  # dump a catalog page for selector debugging
    python bump.py stats              # snapshot per-listing views/chats/favourites
    python bump.py run --publish --experiment  # bump half, hold half as control

Each command opens that Chrome if it is not already running and closes it again
when done, so it never lingers as your everyday browser. If a site asks for a
login anyway, you get a RE-AUTH NEEDED email: run `python bump.py login` again.

launchd runs one agent per site — see the com.enrigle.wallabump.<site>.plist
files — so Wallapop and Vinted get separate nights.
"""

from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import RotatingFileHandler

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .commands import analyze, login, probe, run, stats, status
from .logic import LOG_FILE, ensure_dirs, load_sites
from .report import RunReport, send_email

log = logging.getLogger("wallabump")


def setup_logging(verbose: bool) -> None:
    ensure_dirs()
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-8s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["run", "login", "probe", "status", "stats", "analyze"]
    )
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
    parser.add_argument(
        "--experiment",
        action="store_true",
        help="bump only half the catalog, leaving the rest as a control group",
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
    if args.command == "status":
        return status(chosen)
    if args.command == "stats":
        return stats(chosen)
    if args.command == "analyze":
        return analyze()

    log.info(
        "Starting run (sites=%s, publish=%s, limit=%s, experiment=%s)",
        ", ".join(site.name for site in chosen),
        args.publish,
        args.limit or "all",
        args.experiment,
    )
    try:
        report = run(chosen, args.publish, args.limit, args.experiment)
    except (RuntimeError, PlaywrightError, PlaywrightTimeout, OSError) as exc:
        report = RunReport(fatal=str(exc))
        log.exception("Run aborted")

    send_email(report)
    log.info("Done — %s", report.subject)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
