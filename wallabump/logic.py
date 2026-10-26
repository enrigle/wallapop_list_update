"""Pure logic: the listing edit, schedules, site config, the experiment."""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import plistlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import tomllib

log = logging.getLogger("wallabump")


HOME_DIR = Path.home() / ".wallapop-bump"
PROFILE_DIR = HOME_DIR / "chrome-profile"
LOG_FILE = HOME_DIR / "bump.log"
# The package sits one level below the repo root, where sites.toml lives.
REPO_DIR = Path(__file__).resolve().parent.parent
SITES_FILE = REPO_DIR / "sites.toml"

# One launchd agent per site, so each marketplace gets its own nights. Derived
# from the site name rather than listed here: a third marketplace stays a
# sites.toml entry, the same way every other site-specific thing does.
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Wallapop-only, and deliberately not in sites.toml: no other configured site
# exposes per-listing counters, so a config key would be a setting with exactly
# one possible value. The page is Angular; each listing is one <tsl-item-stats-row>
# holding three .col-counters in the order the header names them.
STATS_URL = "https://es.wallapop.com/app/stats"
STATS_SITE = "wallapop"
STATS_DIR = HOME_DIR / "stats"

# The edit that resurfaces the listing. Swap for a word pair (e.g. two closing
# sentences) if a trailing period ever proves unsuitable; nothing else changes.
MARKER = "."

# Only append the marker after a character that reads naturally before a period.
# Anything else ("Te interesa?", "Talla 42,") would publish visible garbage.
APPENDABLE_TAIL = re.compile(r"[\w)\]\"'»]$", re.UNICODE)

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


def human_delta(seconds: float) -> str:
    """A rough "in 6h 58m" for a future gap. Days once it passes 24 hours."""
    if seconds < 0:
        return "overdue"
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def last_run(log_text: str) -> tuple[str, str] | None:
    """The timestamp and summary of the most recent finished run, if any.

    Reads the log's own "Done — Wallabump: ..." line rather than keeping a
    state file, so status cannot disagree with what actually happened.
    """
    found = None
    for line in log_text.splitlines():
        match = re.match(
            r"^(\d{4}-\d\d-\d\d \d\d:\d\d):\d\d[,.]\d+\s+\w+\s+Done — (.+)$", line
        )
        if match:
            found = (match.group(1), match.group(2).strip())
    return found


def plist_name(site_name: str) -> str:
    """The launchd agent filename for a site."""
    return f"com.enrigle.wallabump.{site_name}.plist"


def plist_for(site_name: str) -> Path:
    """The installed agent for a site, falling back to the repo's copy.

    status() reports on whichever it finds, and says so loudly when only the
    repo copy exists — an uninstalled agent fires nothing at all.
    """
    installed = LAUNCH_AGENTS_DIR / plist_name(site_name)
    return installed if installed.exists() else REPO_DIR / plist_name(site_name)


def schedule_entries(plist_path: Path) -> list[dict[str, int]]:
    """The StartCalendarInterval entries, always as a list."""
    if not plist_path.exists():
        return []
    try:
        with plist_path.open("rb") as handle:
            data = plistlib.load(handle)
    except (plistlib.InvalidFileException, OSError, ValueError):
        return []
    raw = data.get("StartCalendarInterval")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [entry for entry in raw if isinstance(entry, dict)]
    return []


def next_fire(entries: list[dict[str, int]], now: datetime) -> datetime | None:
    """Earliest moment after `now` that any entry fires.

    launchd numbers Sunday as 0 (and accepts 7); Python's isoweekday makes it 7.
    An entry with no Weekday fires every day. An entry without both Hour and
    Minute is skipped rather than guessed at.
    """
    best: datetime | None = None
    for day_offset in range(8):
        day = (now + timedelta(days=day_offset)).date()
        for entry in entries:
            if "Hour" not in entry or "Minute" not in entry:
                continue
            weekday = entry.get("Weekday")
            if weekday is not None:
                wanted = 7 if weekday in (0, 7) else weekday
                if day.isoweekday() != wanted:
                    continue
            # launchd fires on local wall-clock time. Inheriting tzinfo from
            # `now` keeps the comparison valid whether the caller passed a
            # naive local datetime or an aware one.
            moment = datetime(
                day.year,
                day.month,
                day.day,
                entry["Hour"],
                entry["Minute"],
                tzinfo=now.tzinfo,
            )
            if moment > now and (best is None or moment < best):
                best = moment
    return best


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


# The experiment splits the catalog in two: one half gets bumped, the other is
# left alone so there is something to compare against. Without a control group,
# an ordinary quiet week is indistinguishable from the bump doing nothing.
BUMP = "bump"
CONTROL = "control"


def parse_counter(text: str) -> int:
    """The number inside a stats counter cell, or 0 when it is not a number.

    The cell carries an icon plus a number, sometimes thousands-separated. A
    cell that cannot be read must not abort a snapshot of 23 listings, and 0 is
    the honest reading of "no views recorded" — but note it is indistinguishable
    from a genuinely unread cell, so a whole column of zeroes means the selector
    broke, not that nobody looked.
    """
    if not text:
        return 0
    digits = re.sub(r"[^0-9]", "", text)
    return int(digits) if digits else 0


def group_for(item_id: str, week: int) -> str:
    """Which experiment half a listing belongs to in a given ISO week.

    Hashed from the id rather than taken from catalog position, so a listing
    keeps its group as the catalog reorders — a control group that reshuffles
    every run measures nothing. sha256 because Python's own hash() is salted
    per process and would reassign every listing on each run.

    The halves swap every ISO week (crossover): each listing spends some weeks
    bumped and some held back, so it is compared against itself rather than
    against a different, possibly more popular, listing. Both Wallapop nights
    (Thu, Sun) fall in one Mon–Sun ISO week, so a half gets both bumps.

    An id-less listing goes to the control group: the run cannot open an edit
    form for it reliably anyway, so it must not count as bumped.
    """
    if not item_id:
        return CONTROL
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    return BUMP if (int(digest, 16) + week) % 2 == 0 else CONTROL


def this_week() -> int:
    return datetime.now().astimezone().isocalendar().week


def ensure_dirs() -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(HOME_DIR, 0o700)


# The crossover started on this Monday (ISO week 42). Snapshots measuring
# earlier weeks used the old fixed split, or none, and must not be mixed in.
EXPERIMENT_START = date(2026, 10, 12)


def measured_week(snapshot: date) -> int | None:
    """The ISO week a snapshot's weekly counters describe, or None to ignore it.

    Only Monday snapshots count: Wallapop's figure is "Estadísticas de la
    semana", so Monday's reading covers the week that just ended. Other days
    straddle two treatment weeks and would blur bumped into control.
    """
    if snapshot.weekday() != 0:
        return None
    week_start = snapshot - timedelta(days=7)
    if week_start < EXPERIMENT_START:
        return None
    return week_start.isocalendar().week


def paired_diffs(observations: list[tuple[str, int, int]]) -> dict[str, float]:
    """Per listing: mean views in bumped weeks minus mean in control weeks.

    Takes (item_id, week, views). A listing seen only in one arm — sold early,
    listed late — has nothing to be compared with and is left out.
    """
    arms: dict[str, dict[str, list[int]]] = {}
    for item_id, week, views in observations:
        group = group_for(item_id, week)
        arms.setdefault(item_id, {BUMP: [], CONTROL: []})[group].append(views)
    return {
        item_id: sum(a[BUMP]) / len(a[BUMP]) - sum(a[CONTROL]) / len(a[CONTROL])
        for item_id, a in arms.items()
        if a[BUMP] and a[CONTROL]
    }


def sign_flip_p(diffs: list[float]) -> float:
    """Two-sided exact p-value that the mean diff is zero.

    Under "the bump does nothing" each listing's diff is as likely to be
    negative as positive, so every sign pattern is equally likely; the p-value
    is the share of patterns whose mean is at least as extreme as observed.
    """
    # ponytail: exact 2^n enumeration, fine to n≈20; sample patterns beyond that.
    if not diffs:
        return 1.0
    observed = abs(sum(diffs))
    patterns = list(itertools.product((1, -1), repeat=len(diffs)))
    extreme = sum(
        1
        for signs in patterns
        if abs(sum(s * d for s, d in zip(signs, diffs))) >= observed - 1e-9
    )
    return extreme / len(patterns)
