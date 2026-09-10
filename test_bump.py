"""Edge-case tests for the pure logic in wallabump."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bump import (
    MARKER,
    Site,
    describe,
    human_delta,
    last_run,
    load_sites,
    next_fire,
    schedule_entries,
    slept_seconds,
    toggle,
)


@pytest.mark.parametrize("desc", [None, "", "   ", "\n\t  \n"])
def test_empty_descriptions_are_skipped(desc: str | None) -> None:
    assert toggle(desc, 640) is None


def test_appends_marker_when_absent() -> None:
    assert toggle("Zapatillas talla 42", 640) == "Zapatillas talla 42."


def test_removes_marker_when_present() -> None:
    assert toggle("Zapatillas talla 42.", 640) == "Zapatillas talla 42"


def test_round_trip_returns_to_start() -> None:
    once = toggle("Mesa de roble", 640)
    assert once is not None
    assert toggle(once, 640) == "Mesa de roble"


def test_ellipsis_is_skipped_not_mangled() -> None:
    assert toggle("Casi nuevo...", 640) is None
    assert toggle("Dos puntos..", 640) is None


def test_trailing_whitespace_is_normalised_before_toggling() -> None:
    assert toggle("Silla vintage  \n", 640) == "Silla vintage."
    assert toggle("Silla vintage.  \n", 640) == "Silla vintage"


def test_append_exactly_at_limit_is_allowed() -> None:
    # 9 chars + marker == maxlen of 10.
    assert toggle("123456789", 10) == "123456789" + MARKER


def test_append_over_limit_is_skipped() -> None:
    assert toggle("1234567890", 10) is None


def test_removal_is_allowed_even_at_limit() -> None:
    # Shrinking can never overflow, so the limit must not block it.
    assert toggle("123456789.", 10) == "123456789"


def test_unknown_limit_is_treated_as_unlimited() -> None:
    assert toggle("Sin limite conocido", 0) == "Sin limite conocido."
    assert toggle("Sin limite conocido", -1) == "Sin limite conocido."


@pytest.mark.parametrize("desc", ["Te interesa?", "Ganga!", "Talla 42,", "Incluye:"])
def test_other_trailing_punctuation_is_skipped(desc: str) -> None:
    # "Te interesa?." is visible garbage. Skip rather than publish it.
    assert toggle(desc, 640) is None


def test_closing_bracket_and_quote_still_accept_a_marker() -> None:
    assert toggle("Mesa (roble)", 640) == "Mesa (roble)."
    assert toggle('Modelo "Kallax"', 640) == 'Modelo "Kallax".'


def test_describe_reports_an_appended_marker() -> None:
    updated = toggle("Zapatillas talla 42", 640)
    assert updated is not None
    assert describe(updated).startswith("added")


def test_describe_reports_a_removed_marker() -> None:
    updated = toggle("Zapatillas talla 42.", 640)
    assert updated is not None
    assert describe(updated).startswith("removed")


def test_describe_shows_the_last_twenty_characters() -> None:
    text = "Mesa de roble maciza restaurada."
    assert describe(text).endswith(repr(text[-20:]))


def test_describe_shows_short_text_whole() -> None:
    assert describe("Mesa.").endswith(repr("Mesa."))


def test_describe_round_trip_flips_direction() -> None:
    once = toggle("Silla vintage", 640)
    assert once is not None
    twice = toggle(once, 640)
    assert twice is not None
    assert describe(once).startswith("added")
    assert describe(twice).startswith("removed")


# --- Site configuration -------------------------------------------------------

WALLAPOP = Site(
    name="wallapop",
    catalog_url="https://es.wallapop.com/app/catalog/published",
    edit_url="https://es.wallapop.com/app/catalog/edit/{item_id}",
    link_pattern="/item/",
    id_regex="/item/([^/?#]+)",
    card_selector="article, li",
    description_selector="textarea",
    edit_control="editar|edit",
    save_control="guardar|save",
    reserved=("reservado",),
    sold=("vendido",),
)


def test_item_id_is_pulled_from_a_listing_url() -> None:
    href = "https://es.wallapop.com/item/mesa-de-roble-123456"
    assert WALLAPOP.item_id(href) == "mesa-de-roble-123456"


@pytest.mark.parametrize(
    "href",
    [
        "",
        "https://es.wallapop.com/app/catalog/published",
        "https://es.wallapop.com/user/someone",
    ],
)
def test_item_id_is_empty_when_the_url_is_not_a_listing(href: str) -> None:
    # An empty id must fall back to clicking Edit, never to a malformed URL.
    assert WALLAPOP.item_id(href) == ""


def test_query_and_fragment_are_not_part_of_the_id() -> None:
    href = "https://es.wallapop.com/item/mesa-123?utm=x#photos"
    assert WALLAPOP.item_id(href) == "mesa-123"


def test_edit_link_is_built_from_the_id() -> None:
    href = "https://es.wallapop.com/item/mesa-123"
    assert WALLAPOP.edit_link(href) == (
        "https://es.wallapop.com/app/catalog/edit/mesa-123"
    )


def test_link_selector_wraps_the_pattern() -> None:
    assert WALLAPOP.link_selector == 'a[href*="/item/"]'


def test_control_patterns_ignore_case() -> None:
    assert WALLAPOP.edit_pattern.search("EDITAR") is not None
    assert WALLAPOP.save_pattern.search("Guardar cambios") is not None


VINTED = Site(
    name="vinted",
    catalog_url="https://www.vinted.es/member/185114459",
    edit_url="https://www.vinted.es/items/{item_id}/edit",
    link_pattern="/items/",
    id_regex=r"/items/(\d+)",
    card_selector='[data-testid^="product-item-id"]',
    description_selector='textarea[name="description"]',
    edit_control="editar|edit",
    save_control="guardar|save",
    reserved=("reservado",),
    sold=("vendido",),
)


def test_vinted_id_is_the_digits_only() -> None:
    assert VINTED.item_id("https://www.vinted.es/items/9873738631") == "9873738631"


def test_vinted_id_ignores_the_slug_and_query() -> None:
    href = "https://www.vinted.es/items/9883610791-pesas-de-mano?homepage_session_id=x"
    assert VINTED.item_id(href) == "9883610791"


@pytest.mark.parametrize(
    "href",
    [
        "https://www.vinted.es/items/new",
        "https://www.vinted.es/member/items/favourite_list",
    ],
)
def test_vinted_non_listing_links_have_no_id(href: str) -> None:
    # Both match link_pattern but are not listings; collect() drops them on this.
    assert VINTED.item_id(href) == ""


def test_vinted_edit_link_is_built_from_the_id() -> None:
    href = "https://www.vinted.es/items/9873738631-lote"
    assert VINTED.edit_link(href) == ("https://www.vinted.es/items/9873738631/edit")


def test_the_shipped_config_loads() -> None:
    sites = load_sites()
    assert set(sites) == {"wallapop", "vinted"}
    assert sites["wallapop"].link_pattern == "/item/"
    assert sites["vinted"].description_selector == 'textarea[name="description"]'


def test_every_shipped_edit_url_takes_an_item_id() -> None:
    for site in load_sites().values():
        assert "{item_id}" in site.edit_url, site.name


def test_every_shipped_id_regex_has_one_group() -> None:
    import re as _re

    for site in load_sites().values():
        assert _re.compile(site.id_regex).groups == 1, site.name


def test_a_missing_key_names_the_site_and_the_key(tmp_path: Path) -> None:
    config = tmp_path / "sites.toml"
    config.write_text(
        '[vinted]\ncatalog_url = "https://example.test"\n', encoding="utf-8"
    )
    with pytest.raises(RuntimeError) as caught:
        load_sites(config)
    message = str(caught.value)
    assert "vinted" in message
    assert "edit_url" in message


def test_a_missing_config_file_is_reported(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        load_sites(tmp_path / "absent.toml")


def test_an_empty_config_is_rejected(tmp_path: Path) -> None:
    # A config with every site commented out must not run silently over nothing.
    config = tmp_path / "sites.toml"
    config.write_text("# nothing configured yet\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        load_sites(config)


# --- Sleep detection ----------------------------------------------------------


def test_no_sleep_reports_effectively_zero() -> None:
    # Both clocks advanced together: the process ran without a break. The two
    # reads are microseconds apart, so this is near-zero rather than exact.
    mark = (time.time(), time.monotonic())
    assert slept_seconds(mark) < 0.01


def test_a_suspension_shows_up_as_the_wall_clock_running_ahead() -> None:
    # Pretend the wall clock started 10 min ago while monotonic started now:
    # that is exactly the shape of a process suspended for 10 minutes.
    mark = (time.time() - 600.0, time.monotonic())
    assert slept_seconds(mark) == pytest.approx(600.0, abs=2.0)


def test_a_slow_step_without_sleep_is_not_reported_as_sleep() -> None:
    # Both clocks 10 min back: the step really did take 10 min while awake.
    mark = (time.time() - 600.0, time.monotonic() - 600.0)
    assert slept_seconds(mark) == pytest.approx(0.0, abs=2.0)


def test_backwards_drift_never_returns_a_negative() -> None:
    # A wall clock nudged forward by NTP must not read as negative sleep.
    mark = (time.time() + 30.0, time.monotonic())
    assert slept_seconds(mark) == 0.0


# --- status reporting ---------------------------------------------------------


def at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    """A fixed-offset datetime. next_fire() inherits tzinfo from the caller,
    so any single zone makes these cases deterministic."""
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# Thursday and Sunday at 21:55, matching the shipped plist.
SCHEDULE = [
    {"Weekday": 4, "Hour": 21, "Minute": 55},
    {"Weekday": 0, "Hour": 21, "Minute": 55},
]


def test_next_fire_finds_tonight_when_it_is_still_ahead() -> None:
    # Thursday afternoon: tonight's 21:55 is the next one.
    now = at(2026, 9, 10, 14, 57)
    assert next_fire(SCHEDULE, now) == at(2026, 9, 10, 21, 55)


def test_next_fire_rolls_to_sunday_once_thursday_has_passed() -> None:
    # Thursday 22:30, just after the run: Sunday is next.
    now = at(2026, 9, 10, 22, 30)
    assert next_fire(SCHEDULE, now) == at(2026, 9, 13, 21, 55)


def test_next_fire_treats_launchd_sunday_seven_like_zero() -> None:
    # launchd accepts both 0 and 7 for Sunday; they must behave identically.
    now = at(2026, 9, 10, 22, 30)
    sunday_as_seven = [{"Weekday": 7, "Hour": 21, "Minute": 55}]
    assert next_fire(sunday_as_seven, now) == at(2026, 9, 13, 21, 55)


def test_next_fire_is_exclusive_of_the_current_minute() -> None:
    # Firing "now" is in the past for scheduling purposes, not the next run.
    now = at(2026, 9, 10, 21, 55)
    assert next_fire(SCHEDULE, now) == at(2026, 9, 13, 21, 55)


def test_an_entry_without_a_weekday_fires_daily() -> None:
    now = at(2026, 9, 10, 14, 0)
    assert next_fire([{"Hour": 21, "Minute": 55}], now) == at(2026, 9, 10, 21, 55)


@pytest.mark.parametrize("entries", [[], [{"Weekday": 4}], [{"Hour": 21}]])
def test_next_fire_returns_none_rather_than_guessing(
    entries: list[dict[str, int]],
) -> None:
    # An incomplete entry must not be filled in with an invented hour.
    assert next_fire(entries, at(2026, 9, 10, 14, 0)) is None


def test_schedule_entries_reads_the_shipped_plist() -> None:
    entries = schedule_entries(Path("com.enrigle.wallabump.plist"))
    assert len(entries) == 2
    assert {e["Hour"] for e in entries} == {21}
    assert {e["Weekday"] for e in entries} == {0, 4}


def test_schedule_entries_on_a_missing_file_is_empty() -> None:
    assert schedule_entries(Path("no-such.plist")) == []


def test_schedule_entries_on_a_corrupt_file_is_empty(tmp_path: Path) -> None:
    # A damaged plist must not crash a status check.
    bad = tmp_path / "bad.plist"
    bad.write_text("this is not a plist", encoding="utf-8")
    assert schedule_entries(bad) == []


def test_last_run_takes_the_most_recent_done_line() -> None:
    log = (
        "2026-09-06 17:47:04,302 INFO     Done — Wallabump: 4 ok, 0 failed\n"
        "2026-09-10 14:40:12,001 INFO     Starting run (sites=wallapop)\n"
        "2026-09-10 14:41:55,900 INFO     Done — Wallabump: 2 ok, 0 failed\n"
    )
    assert last_run(log) == ("2026-09-10 14:41", "Wallabump: 2 ok, 0 failed")


def test_last_run_on_a_log_with_no_finished_run() -> None:
    assert last_run("2026-09-10 14:40:12,001 INFO     Starting run\n") is None


def test_last_run_on_an_empty_log() -> None:
    assert last_run("") is None


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0m"),
        (90, "1m"),
        (3600, "1h 00m"),
        (25140, "6h 59m"),
        (259200, "3d 0h"),
        (-60, "overdue"),
    ],
)
def test_human_delta(seconds: float, expected: str) -> None:
    assert human_delta(seconds) == expected
