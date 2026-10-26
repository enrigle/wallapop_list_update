"""What the bot does on a page: find listings, edit one, read the stats."""

from __future__ import annotations

import logging
from typing import Any

from playwright.sync_api import (
    Locator,
    Page,
    sync_playwright,
)
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from .chrome import attach, owned_chrome
from .logic import STATS_URL, Site, describe, toggle
from .report import Item, Result

log = logging.getLogger("wallabump")


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


_STATS_JS = """
() => {
  const rows = [...document.querySelectorAll('tsl-item-stats-row')];
  return rows.map(row => {
    const link = row.querySelector('a[href*="/item/"]');
    const text = sel => {
      const el = row.querySelector(sel);
      return el ? (el.innerText || '').trim() : '';
    };
    return {
      href: link ? link.href : '',
      title: text('.title'),
      price: text('.price'),
      published: text('.col-date'),
      counters: [...row.querySelectorAll('.col-counters')]
                  .map(c => (c.innerText || '').trim()),
    };
  });
}
"""


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
    if site.edit_url and site.item_id(item.href):
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
    control.first.click(timeout=15000)
    return find_textarea(page, site, timeout=15000)


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


def read_stats_page() -> list[dict[str, Any]] | None:
    """One attempt at reading the stats page.

    Returns None when signed out, [] when no rows rendered. Raises on browser
    errors and timeouts; the caller decides whether to try again.
    """
    with owned_chrome(STATS_URL), sync_playwright() as playwright:
        browser = attach(playwright, STATS_URL)
        page = browser.contexts[0].new_page()
        try:
            page.goto(STATS_URL, wait_until="domcontentloaded", timeout=60000)
            if "auth" in page.url or "login" in page.url:
                return None
            page.wait_for_selector("tsl-item-stats-row", timeout=30000)
            # The list renders lazily; scroll until the row count stops growing.
            seen = -1
            for _ in range(15):
                found = page.locator("tsl-item-stats-row").count()
                if found == seen:
                    break
                seen = found
                page.mouse.wheel(0, 5000)
                page.wait_for_timeout(800)
            if "auth" in page.url or "login" in page.url:
                return None
            rows: list[dict[str, Any]] = page.evaluate(_STATS_JS)
            return rows
        finally:
            page.close()
