"""Edge-case tests for the only piece of pure logic in wallabump."""

from __future__ import annotations

import pytest

from bump import MARKER, describe, toggle


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
