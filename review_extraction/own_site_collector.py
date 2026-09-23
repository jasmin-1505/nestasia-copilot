"""
Playwright-based SKU collector for nestasia.in itself (not a competitor
marketplace) -- covers all 5 Kitchen subcategories: Cookware, Bakeware,
Container, Lunch Boxes+Bags, Kitchen Racks+Trivets.

Per-SKU, this captures price/discount/material-type info AND, deliberately
as two separate fields, the collection page's stock badge (`data-available`
on the quick-add button) vs. the Add-to-Cart button's own rendered text.
Prior research on this project found these two disagree site-wide on
nestasia.in -- this script's job is to catch that automatically, not just
collect prices. See the printed summary at the end of each run.

Install (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python3 own_site_collector.py
Reads nestasia_subcategories.csv (one row per subcategory+collection-URL --
a subcategory can span more than one URL where nestasia.in has no single
combined collection page, e.g. Lunch Boxes+Bags = lunch-boxes + lunch-bags).
Appends a fresh snapshot to nestasia_catalogue.csv on every run -- this is
NOT incremental like extract_reviews.py. Price and stock state are
point-in-time facts that change day to day, so every run's rows are kept
(with their own collected_at timestamp) rather than deduped against past
runs. If you want a "latest state only" view, filter the output CSV by
max(collected_at) per product_url yourself.
"""

import csv
import os
import re
import time
import random
import logging
from datetime import datetime
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SUBCATEGORIES_FILE = "nestasia_subcategories.csv"
OUTPUT_FILE = "nestasia_catalogue.csv"
PAGE_LOAD_TIMEOUT_MS = 20000
SOURCE_TYPE = "live_storefront"

# Same pacing tuned for discover_products_playwright.py's Flipkart crawling,
# reused here rather than invented fresh: a live check today (Sep 2026) showed
# nestasia.in also serves HTTP 403 after a rapid burst of requests without
# delay, so the same conservative-pacing rationale applies to this site too.
DELAY_BETWEEN_PAGES_SECONDS = 9       # more conservative pacing -- lowers detection risk with a real browser
# Raised from 6 (Sep 2026): the known prior baselines for Container and Lunch
# Boxes+Bags (~99, ~77, 39, 50 SKUs per their own on-page counters -- see
# NOTES.md finding (g)) need up to ~9 pages at ~12 SKUs/page. At the old cap
# of 6, every one of those subcategories would hit the page-cap-truncation
# branch below regardless of whether the block has cleared, making a genuine
# complete collection impossible to reach even on a clean run. 12 gives
# margin above the largest known baseline without being unbounded -- a
# genuine 0-new-page stop still ends the loop early on small subcategories,
# this only matters for ones that actually have more pages to give.
MAX_PAGES_PER_SUBCATEGORY = 12

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Card extraction
# ---------------------------------------------------------------------------
# Runs inside the browser: reads each product card's declared stock tag
# (`data-available` on the quick-add button -- what the collection page
# "says") separately from which button-label span is actually rendered
# visible (what a shopper actually sees). We use getComputedStyle rather
# than checking for a "hidden" class name, because a class-name check is a
# fragile coincidental signal (see extract_reviews.py's _detect_amazon_block
# for the same lesson learned the hard way with Amazon's "captcha" substring).
#
# Cards are scoped to #ProductGridContainer, the real collection grid --
# NOT a plain "[data-product-card]" query. nestasia.in's predictive-search
# drawer (#SearchDrawerDefault > .recommended-products) is present in the
# DOM on every page, sitewide, and reuses the exact same [data-product-card]
# card markup for its fixed "recommended products" list. It is not inside a
# <header> tag (that guess didn't work -- verified by walking the real
# ancestor chain), so it must be excluded by positively scoping into the
# grid container rather than blocklisting the search drawer, since there
# could be other sitewide widgets like it we haven't found yet. Confirmed
# Sep 2026 while testing the Trivets collection: the same 10 search-drawer
# cards showed up ahead of the real trivets, and had already silently
# inflated the earlier Cookware SKU count the same way.
_CARD_EXTRACT_JS = """
els => els.filter(el => el.closest('#ProductGridContainer')).map(el => {
    const anchor = el.querySelector("a[href*='/products/']");
    const btn = el.querySelector('[data-available]');
    let visibleButtonText = null;
    let buttonDisabled = null;
    if (btn) {
        buttonDisabled = btn.disabled;
        const spans = Array.from(btn.querySelectorAll('span'))
            .map(s => {
                const cs = getComputedStyle(s);
                return {text: s.innerText.trim(), hidden: cs.display === 'none' || cs.visibility === 'hidden'};
            })
            .filter(s => s.text && !s.hidden);
        if (spans.length) visibleButtonText = spans.map(s => s.text).join(' / ');
    }
    return {
        title: el.getAttribute('data-product-title'),
        href: anchor ? anchor.getAttribute('href') : null,
        priceAttr: el.getAttribute('data-product-price'),
        priceText: (el.querySelector('.price') || el.querySelector('[class*=price]') || {}).innerText || '',
        materialTypeTag: el.getAttribute('data-product-category'),
        dataAvailable: btn ? btn.getAttribute('data-available') : null,
        buttonDisabled: buttonDisabled,
        visibleButtonText: visibleButtonText,
    };
})
"""

DISCOUNT_RE = re.compile(r"\((\d+)%\s*Off\)", re.IGNORECASE)


def _parse_discount_percent(price_text):
    match = DISCOUNT_RE.search(price_text or "")
    return match.group(1) if match else None


def _stock_status_tag(data_available):
    if data_available == "true":
        return "In Stock"
    if data_available == "false":
        return "Out of Stock"
    return "Unknown"


def _actual_button_state(visible_button_text, button_disabled):
    if visible_button_text:
        return visible_button_text
    if button_disabled:
        return "Sold Out"  # fallback signal when no visible span text was found at all
    return "Unknown"


def _detect_nestasia_block(status_code, page_title):
    """
    Returns a reason string if this page load looks like nestasia.in's
    Cloudflare bot-check interstitial rather than the real collection page,
    else None.

    Confirmed (Sep 2026, see NOTES.md finding (g)): a blocked page-2+
    request returned HTTP 403 with page title literally "Just a moment..."
    (Cloudflare's challenge page), not the real collection page --
    #ProductGridContainer then naturally found 0 cards, which the old logic
    silently read as "no more products, stop here." Same lesson as
    _detect_amazon_block in extract_reviews.py: check for the actual
    interstitial marker, not just the absence of expected content.
    """
    if status_code is not None and status_code != 200:
        return f"non-200 status ({status_code})"
    if page_title and "just a moment" in page_title.lower():
        return f"Cloudflare challenge page (title={page_title!r})"
    return None


def collect_subcategory(page, category_url, subcategory):
    """Paginate one nestasia.in collection page, same stop-on-empty-page
    pattern as discover_flipkart_products() in discover_products_playwright.py.

    Returns (found, collection_complete). collection_complete is False when
    pagination was cut short by a detected block rather than a genuine last
    page -- see _detect_nestasia_block and NOTES.md finding (g). A blocked
    page must never be silently recorded as "no more products, done."
    """
    found = {}  # product_url -> row dict, dedup within this run by URL
    collection_complete = True
    hit_page_cap_with_more_available = False

    for page_num in range(1, MAX_PAGES_PER_SUBCATEGORY + 1):
        separator = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{separator}page={page_num}" if page_num > 1 else category_url

        try:
            resp = page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # give the product grid a moment to render
        except Exception as e:
            log.warning(f"  page {page_num} load failed: {e}")
            collection_complete = False
            break

        block_reason = _detect_nestasia_block(resp.status if resp else None, page.title())
        if block_reason:
            log.warning(
                f"  BLOCKED on page {page_num} for {subcategory} (reason: {block_reason}) -- "
                f"stopping here. {len(found)} SKU(s) collected so far are PARTIAL, not the "
                f"full collection. This is NOT the same as 'no more products.'"
            )
            collection_complete = False
            break

        cards = page.eval_on_selector_all("[data-product-card]", _CARD_EXTRACT_JS)

        new_this_page = 0
        for card in cards:
            if not card.get("href") or not card.get("title"):
                continue
            product_url = urljoin("https://nestasia.in", card["href"].split("?")[0])
            if product_url in found:
                continue

            stock_status_tag = _stock_status_tag(card.get("dataAvailable"))
            actual_button_state = _actual_button_state(card.get("visibleButtonText"), card.get("buttonDisabled"))

            found[product_url] = {
                "subcategory": subcategory,
                "product_name": card["title"][:150],
                "product_url": product_url,
                "price": card.get("priceAttr"),
                "discount_percent": _parse_discount_percent(card.get("priceText")),
                "material_type_tag": card.get("materialTypeTag"),
                "stock_status_tag": stock_status_tag,
                "actual_button_state": actual_button_state,
                "stock_mismatch": _is_mismatch(stock_status_tag, actual_button_state),
            }
            new_this_page += 1

        log.info(f"  page {page_num}: {new_this_page} new SKUs (running total {len(found)})")

        if new_this_page == 0:
            log.info(f"  no new SKUs on page {page_num}, genuine end of collection for {subcategory}")
            break

        if page_num == MAX_PAGES_PER_SUBCATEGORY:
            # Loop is about to exit because we hit the page cap, not because
            # this page returned 0 new SKUs -- that means there is very
            # likely a page (MAX_PAGES_PER_SUBCATEGORY + 1) with more real
            # products on it. Confirmed as a real bug shape in
            # competitor_collector.py's Wonderchef build (NOTES.md finding
            # (h)): a page-cap truncation silently looked identical to a
            # genuine full collection until checked. Container and Lunch
            # Boxes+Bags subcategories are exactly the ones with prior
            # baselines (~99, ~77, 39, 50 SKUs) that could plausibly exceed
            # MAX_PAGES_PER_SUBCATEGORY=6 pages at ~12 SKUs/page, so this
            # check matters here, not just hypothetically.
            hit_page_cap_with_more_available = True
            log.warning(
                f"  hit MAX_PAGES_PER_SUBCATEGORY={MAX_PAGES_PER_SUBCATEGORY} cap while page {page_num} still "
                f"returned {new_this_page} new SKUs for {subcategory} -- more pages likely exist beyond the cap. "
                f"{len(found)} SKU(s) collected so far are PARTIAL, not the full collection."
            )
            break

        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))

    if hit_page_cap_with_more_available:
        collection_complete = False

    return found, collection_complete


def _is_mismatch(stock_status_tag, actual_button_state):
    """True when the declared tag and the rendered button disagree on
    availability -- this is the bug this script exists to catch."""
    tag_says_in_stock = stock_status_tag == "In Stock"
    button_says_in_stock = actual_button_state.lower().startswith("add to cart") if actual_button_state else None
    if button_says_in_stock is None or stock_status_tag == "Unknown":
        return "Unknown"
    return tag_says_in_stock != button_says_in_stock


# ---------------------------------------------------------------------------
# Output dataset handling
# ---------------------------------------------------------------------------
FIELDNAMES = [
    "subcategory", "product_name", "product_url", "price", "discount_percent",
    "material_type_tag", "stock_status_tag", "actual_button_state",
    "stock_mismatch", "collection_complete", "source_type", "collected_at",
]


def append_catalogue(rows):
    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    if not os.path.exists(SUBCATEGORIES_FILE):
        log.error(f"{SUBCATEGORIES_FILE} not found.")
        return

    per_subcategory_counts = {}
    partial_subcategories = set()
    mismatches = []
    all_rows = []
    now = datetime.now().isoformat()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-IN",
        )
        page = context.new_page()

        with open(SUBCATEGORIES_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                subcategory = row["subcategory"].strip()
                category_url = row["url"].strip()

                log.info(f"Collecting {subcategory} ({category_url})...")
                found, collection_complete = collect_subcategory(page, category_url, subcategory)
                status_note = "" if collection_complete else " *** PARTIAL -- blocked mid-pagination ***"
                log.info(f"  -> {len(found)} SKUs found for {subcategory}{status_note}")

                if not collection_complete:
                    partial_subcategories.add(subcategory)

                per_subcategory_counts[subcategory] = per_subcategory_counts.get(subcategory, 0) + len(found)

                for product_url, sku in found.items():
                    sku["source_type"] = SOURCE_TYPE
                    sku["collected_at"] = now
                    sku["collection_complete"] = collection_complete
                    all_rows.append(sku)
                    if sku["stock_mismatch"] is True:
                        mismatches.append(sku)

        browser.close()

    if all_rows:
        append_catalogue(all_rows)

    print()
    print("=" * 60)
    print("Nestasia.in catalogue collection summary")
    print("=" * 60)
    for subcategory, count in per_subcategory_counts.items():
        flag = "  *** PARTIAL -- blocked mid-pagination, NOT a full count ***" if subcategory in partial_subcategories else ""
        print(f"  {subcategory}: {count} SKUs{flag}")
    print(f"  Total SKUs collected this run: {len(all_rows)}")
    if partial_subcategories:
        print()
        print(f"WARNING: {len(partial_subcategories)} subcategory(ies) hit a block mid-pagination and "
              f"are INCOMPLETE, not a true collection count: {', '.join(sorted(partial_subcategories))}")
    print()
    if mismatches:
        print(f"STOCK-STATE MISMATCHES FOUND: {len(mismatches)}")
        for m in mismatches:
            print(f"  - [{m['subcategory']}] {m['product_name']}: "
                  f"tag='{m['stock_status_tag']}' vs button='{m['actual_button_state']}' -- {m['product_url']}")
    else:
        print("No stock-state mismatches found in this run.")
    print("=" * 60)


if __name__ == "__main__":
    main()
