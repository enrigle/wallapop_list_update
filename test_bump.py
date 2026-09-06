"""Edge-case tests for the pure logic in wallabump."""

from __future__ import annotations

from pathlib import Path

import pytest

from bump import MARKER, Site, describe, load_sites, toggle


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
