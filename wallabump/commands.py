"""The commands behind bump.py: run, login, status, probe, stats, analyze."""

from __future__ import annotations

import csv
import logging
import os
import random
import subprocess
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    Page,
    sync_playwright,
)
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .chrome import (
    CDP_URL,
    SLEEP_DRIFT_SECONDS,
    attach,
    cdp_reachable,
    clock_mark,
    owned_chrome,
    slept_seconds,
)
from .logic import (
    BUMP,
    CONTROL,
    EXPERIMENT_START,
    HOME_DIR,
    LAUNCH_AGENTS_DIR,
    LOG_FILE,
    PROFILE_DIR,
    STATS_DIR,
    STATS_SITE,
    STATS_URL,
    Site,
    ensure_dirs,
    group_for,
    human_delta,
    last_run,
    measured_week,
    next_fire,
    paired_diffs,
    parse_counter,
    plist_for,
    plist_name,
    schedule_entries,
    sign_flip_p,
    this_week,
)
from .pages import bump_item, collect, is_logged_in, read_stats_page
from .report import Result, RunReport

log = logging.getLogger("wallabump")


# Human-ish gap between listings. A whole catalog edited in 30s is a bot.
PACE_SECONDS = (20.0, 90.0)


def probe_file(site: Site) -> Path:
    return HOME_DIR / f"probe-{site.name}.html"


def bump_site(
    page: Page,
    browser: Browser,
    site: Site,
    publish: bool,
    limit: int,
    experiment: bool = False,
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

    # The experiment leaves half the catalog untouched so the bumped half has
    # something to be compared against. Held-back listings are recorded as
    # skipped rather than dropped, so the email still accounts for every one.
    if experiment:
        week = this_week()
        held = [i for i in active if group_for(site.item_id(i.href), week) != BUMP]
        for item in held:
            results.append(Result(site.name, item.title, "skipped", CONTROL))
        active = [i for i in active if group_for(site.item_id(i.href), week) == BUMP]
        log.info(
            "%s: experiment on — %d to bump, %d held back",
            site.name,
            len(active),
            len(held),
        )

    if limit > 0:
        active = active[:limit]

    mark = clock_mark()
    for index, item in enumerate(active):
        try:
            result = bump_item(page, site, item, publish)
        except (PlaywrightError, PlaywrightTimeout, ValueError) as exc:
            result = Result(site.name, item.title, "failed", type(exc).__name__)
        log.info("%s: %s %s", item.title, result.status, result.reason)
        results.append(result)

        if page.is_closed() or not browser.is_connected():
            return results, "Browser connection lost mid-run (Mac asleep?)"

        # On battery a scheduled wake is a DarkWake: a few seconds of CPU, then
        # back to sleep. Without this the run limps on for hours, one item per
        # wake, and reports nonsense. Nothing is lost by stopping — there is no
        # state file, so the next run reads the live descriptions and continues.
        asleep = slept_seconds(mark)
        if asleep > SLEEP_DRIFT_SECONDS:
            return results, (
                f"Mac slept for {asleep / 60:.0f} min mid-run; abandoning. "
                "Scheduled wake on battery only gives a DarkWake."
            )
        mark = clock_mark()

        if index < len(active) - 1:
            time.sleep(random.uniform(*PACE_SECONDS))

    return results, ""


def run(
    sites: list[Site], publish: bool, limit: int, experiment: bool = False
) -> RunReport:
    report = RunReport()
    ensure_dirs()

    # A full catalog takes ~30 min at human pace; idle sleep mid-run kills the
    # CDP connection. caffeinate -w exits by itself when this process does.
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])

    with owned_chrome(sites[0].catalog_url), sync_playwright() as playwright:
        browser = attach(playwright, sites[0].catalog_url)
        context = browser.contexts[0]
        page = context.new_page()
        try:
            for site in sites:
                results, fatal = bump_site(
                    page, browser, site, publish, limit, experiment
                )
                report.results.extend(results)
                if fatal:
                    report.fatal = fatal
                    log.error(fatal)
                    return report
        finally:
            try:
                page.close()  # our tab; owned_chrome closes the browser if we opened it
            except PlaywrightError:
                pass  # connection already gone; nothing left to close

    return report


def login(sites: list[Site]) -> int:
    """Open a remote-debuggable Chrome, let you sign in, then verify each site.

    One profile carries every site's session, so this checks them all and names
    the ones still needing a sign-in. A Chrome this opened is closed at the end;
    the cookies stay in the profile for the next run.
    """
    if cdp_reachable():
        log.info("Chrome with remote debugging is already running.")
    else:
        log.info("Opening Chrome — sign in, then come back here.")

    names = ", ".join(site.name for site in sites)
    missing: list[str] = []
    with owned_chrome(sites[0].catalog_url):
        input(f"Press Enter once you are signed in to {names}… ")

        if PROFILE_DIR.exists():
            os.chmod(PROFILE_DIR, 0o700)

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

    log.info("All sessions confirmed. Runs open Chrome themselves and close it after.")
    return 0


def status(sites: list[Site]) -> int:
    """Report whether each site is still signed in, without editing anything.

    Exits 0 when every site is reachable and signed in, 1 otherwise, so it can
    be used as a check rather than only read by eye.
    """
    ensure_dirs()
    was_running = cdp_reachable()
    log.info(
        "Chrome    %s",
        f"running on {CDP_URL}" if was_running else "not running (starting it)",
    )

    signed_out: list[str] = []
    with owned_chrome(sites[0].catalog_url), sync_playwright() as playwright:
        browser = attach(playwright, sites[0].catalog_url)
        page = browser.contexts[0].new_page()
        try:
            for site in sites:
                page.goto(site.catalog_url, wait_until="domcontentloaded")
                if not is_logged_in(page, site):
                    log.info("%-9s signed OUT — run: python bump.py login", site.name)
                    signed_out.append(site.name)
                    continue
                # Same collector the run uses, so the count cannot disagree
                # with what a real run would find.
                log.info(
                    "%-9s signed in  (%d listings)", site.name, len(collect(page, site))
                )
        finally:
            page.close()

    previous = (
        last_run(LOG_FILE.read_text(encoding="utf-8")) if LOG_FILE.exists() else None
    )
    log.info("")
    if previous is None:
        log.info("Last run  never")
    else:
        log.info("Last run  %s  %s", previous[0], previous[1])

    # Local time on purpose: launchd fires on the wall clock, so an aware
    # local datetime is what the schedule actually means.
    now = datetime.now().astimezone()
    for site in sites:
        plist = plist_for(site.name)
        upcoming = next_fire(schedule_entries(plist), now)
        if upcoming is None:
            log.info("Next run  %-9s no schedule found in %s", site.name, plist)
            continue
        log.info(
            "Next run  %-9s %s  (in %s)",
            site.name,
            upcoming.strftime("%a %H:%M"),
            human_delta((upcoming - now).total_seconds()),
        )
        if not (LAUNCH_AGENTS_DIR / plist_name(site.name)).exists():
            log.info("          NOT INSTALLED — nothing will fire. Copy the plist.")

    return 1 if signed_out else 0


def probe(sites: list[Site]) -> int:
    """Dump each catalog page so selectors can be fixed after a redesign."""
    ensure_dirs()
    with owned_chrome(sites[0].catalog_url), sync_playwright() as playwright:
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


STATS_ATTEMPTS = 3
STATS_RETRY_SECONDS = 120


def stats(sites: list[Site]) -> int:
    """Snapshot each listing's views, chats and favourites to a CSV.

    Strictly read-only: it opens one page, reads numbers, and never touches an
    edit form. The counters are Wallapop's own "Estadísticas de la semana", so
    they are a rolling weekly figure rather than a lifetime total — fine for
    comparing two groups at the same moment, misleading if read as all-time.
    """
    chosen = [site for site in sites if site.name == STATS_SITE]
    if not chosen:
        log.error("stats only covers %s, which is not in this run.", STATS_SITE)
        return 2
    site = chosen[0]

    ensure_dirs()
    STATS_DIR.mkdir(parents=True, exist_ok=True)

    # The stats page often times out or renders empty on the first try; a
    # missed Monday snapshot loses a whole experiment week, so try again.
    # Each attempt opens and closes its own Chrome, which also clears a
    # frozen tab.
    raw: list[dict[str, Any]] | None = []
    for attempt in range(1, STATS_ATTEMPTS + 1):
        try:
            raw = read_stats_page()
        except (PlaywrightError, PlaywrightTimeout, RuntimeError) as exc:
            log.warning("stats attempt %d/%d failed: %s", attempt, STATS_ATTEMPTS, exc)
            raw = []
        if raw is None:
            log.error("RE-AUTH NEEDED — run: python bump.py login")
            return 1
        if raw:
            break
        if attempt < STATS_ATTEMPTS:
            log.info("No stats rows yet; retrying in %ds.", STATS_RETRY_SECONDS)
            time.sleep(STATS_RETRY_SECONDS)

    if not raw:
        log.error(
            "No listings found on %s after %d attempts — stale selector or page "
            "not loaded.",
            STATS_URL,
            STATS_ATTEMPTS,
        )
        return 1

    target = STATS_DIR / f"{datetime.now().astimezone():%Y-%m-%d-%H%M}.csv"
    # The group column is this week's assignment, i.e. the treatment the
    # counters are about to accumulate — not the week they already reflect.
    # `analyze` recomputes the group for the week each snapshot measures.
    week = this_week()
    bumped = 0
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "item_id",
                "title",
                "price",
                "published",
                "views",
                "chats",
                "favourites",
                "group",
                "iso_week",
            ]
        )
        for row in raw:
            counters = list(row.get("counters") or [])
            counters += [""] * (3 - len(counters))
            item_id = site.item_id(row.get("href", ""))
            group = group_for(item_id, week)
            bumped += group == BUMP
            writer.writerow(
                [
                    item_id,
                    row.get("title", ""),
                    row.get("price", ""),
                    row.get("published", ""),
                    parse_counter(counters[0]),
                    parse_counter(counters[1]),
                    parse_counter(counters[2]),
                    group,
                    week,
                ]
            )

    log.info("Wrote %d listings to %s", len(raw), target)
    log.info("Split: %d %s, %d %s", bumped, BUMP, len(raw) - bumped, CONTROL)
    return 0


def analyze() -> int:
    """Read the stats CSVs and report whether bumped weeks beat control weeks."""
    observations: list[tuple[str, int, int]] = []
    latest: dict[int, Path] = {}
    if STATS_DIR.exists():
        for path in sorted(STATS_DIR.glob("*.csv")):
            try:
                taken = date.fromisoformat(path.stem[:10])  # 2026-10-19-2000.csv
            except ValueError:
                continue
            week = measured_week(taken)
            if week is not None:
                latest[week] = path  # sorted, so the last one of a Monday wins
    for week, path in sorted(latest.items()):
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                views = row.get("views", "")
                if row.get("item_id") and views.isdigit():
                    observations.append((row["item_id"], week, int(views)))

    diffs = paired_diffs(observations)
    if not diffs:
        log.error(
            "No listing has both a bumped and a control week yet. Needs Monday "
            "snapshots from %s on, covering at least two weeks (found %d).",
            EXPERIMENT_START,
            len(latest),
        )
        return 1

    for item_id, diff in sorted(diffs.items(), key=lambda kv: kv[1]):
        print(f"{diff:+8.1f}  {item_id}")
    effect = sum(diffs.values()) / len(diffs)
    p = sign_flip_p(list(diffs.values()))
    print(
        f"\nWeeks: {', '.join(str(w) for w in sorted(latest))}  "
        f"Listings: {len(diffs)}\n"
        f"Mean views per week, bumped minus control: {effect:+.1f}  (p = {p:.3f})"
    )
    return 0
