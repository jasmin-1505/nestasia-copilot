"""
Playwright-based SKU collector for competitor brand storefronts (Home Centre,
Milton, Prestige, Wonderchef, Borosil) -- as opposed to own_site_collector.py,
which covers nestasia.in itself.

Built following the same conventions established in own_site_collector.py,
from the start rather than patched on afterward:
  - config-driven input (competitor_sites.csv), no URLs hardcoded in the script
  - block/interstitial detection wired into every page load, not just added
    after a run silently under-counts (see own_site_collector.py NOTES.md
    finding (g) -- that bug is the reason this script checks status/title
    on every single page fetch, from the first line of code)
  - collection_complete flag: False whenever pagination was cut short by a
    detected block, so a partial run can never be silently mistaken for a
    genuine full collection
  - source_type="live_storefront" and a collected_at timestamp on every row,
    matching own_site_collector.py's output schema

Each competitor brand's site is a different platform under the hood, so
BOTH the card selectors AND the pagination mechanics are dispatched by the
`platform` column in competitor_sites.csv (PLATFORM_COLLECTORS) rather than
assumed to be one shared shape. Implemented so far:
  - "shopify_t4s" (confirmed against Wonderchef) -- same Shopify theme
    family as nestasia.in, URL-query-param pagination (?page=N).
  - "unbxd_nextjs" (confirmed against Home Centre) -- Next.js + Material-UI
    frontend backed by an Unbxd search API, click-triggered pagination with
    the response intercepted over the network rather than read from the DOM
    or a URL param. See that platform's section below for why its
    stock-mismatch column is "N/A" rather than True/False.
  - "shopify_hyper_sections" (confirmed against Milton) -- Shopify's "Hyper"
    theme, a third distinct Shopify layout in this project. Pagination uses
    Shopify's Section Rendering API (?page=N&section_id=<id>) rather than a
    plain URL param or a click-and-intercept. This is the first platform
    where a genuine stock-mismatch check succeeded end-to-end (confirmed
    real mismatches against product-page ground truth) -- see NOTES.md
    finding (k).
  - "magento_luma" (confirmed against Prestige/TTK Prestige) -- Magento with
    the Amasty layered-navigation extension, a fourth distinct platform.
    Plain ?p=N pagination, no default availability filter (confirmed the
    same rigorous way as Wonderchef's absence, not assumed), and a stock
    signal (grid-level .stock.unavailable badge) that only ever renders for
    genuinely out-of-stock products -- see NOTES.md finding (m).
  - "shopify_borosil_revamp" (confirmed against Borosil/myborosil.com) --
    Shopify again, but a fifth distinct custom theme sharing no markup with
    any of the above. Plain ?page=N pagination with a two-independent-signal
    completeness check built in from the start. No default availability
    filter (an "Exclude Out Of Stock" checkbox exists but is unchecked by
    default, confirmed both from its DOM state and behaviorally). Ground-
    truth-confirmed real stock-mismatches -- see NOTES.md finding (n).
All five originally-scoped competitor brands now have a working platform
collector.

Install (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python competitor_collector.py
Reads competitor_sites.csv (one row per brand+category+collection-URL).
Appends a fresh snapshot to competitor_catalogue.csv on every run -- same
non-incremental, point-in-time-facts rationale as own_site_collector.py.
"""

import csv
import os
import time
import random
import logging
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SITES_FILE = "competitor_sites.csv"
OUTPUT_FILE = "competitor_catalogue.csv"
PAGE_LOAD_TIMEOUT_MS = 20000
SOURCE_TYPE = "live_storefront"

# Same conservative pacing rationale as own_site_collector.py: nestasia.in,
# Flipkart, and Amazon.in were all separately confirmed (see that script's
# NOTES.md) to have some form of burst-based bot protection. No competitor
# site has been proven safe from the same pattern just because it hasn't
# been hit yet, so the same defaults are used here rather than assuming a
# competitor's own site is unprotected.
DELAY_BETWEEN_PAGES_SECONDS = 9
# Raised from 6 (Sep 2026, adding Prestige): Prestige's Cookware category
# alone declares 321 products at 16/page, needing ~21 pages plus one more to
# confirm genuine exhaustion. Same principle as own_site_collector.py's
# MAX_PAGES_PER_SUBCATEGORY increase -- a cap smaller than a real known
# catalogue size makes genuine completion structurally impossible regardless
# of whether the page-cap-truncation vs. genuine-end distinction is coded
# correctly, so the cap has to actually cover the real data before that
# distinction can ever resolve to True.
MAX_PAGES_PER_CATEGORY = 25

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Block / interstitial detection
# ---------------------------------------------------------------------------
# Generic across brands: a non-200 status is always suspect, and these page
# titles are common bot-check/interstitial wording seen across Cloudflare,
# Akamai, and similar challenge pages (not just nestasia.in's "Just a
# moment..." -- own_site_collector.py's NOTES.md (g) explicitly warns not to
# assume a clean run means a site is unprotected). This list is a starting
# point, not exhaustive -- extend it the first time a competitor site is
# found serving something not caught here.
_INTERSTITIAL_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "access denied",
    "are you a human",
    "verify you are a human",
    "robot check",
    "please enable cookies",
    "checking your browser",
)


def _detect_block(status_code, page_title):
    """Returns a reason string if this page load looks like a block or
    interstitial rather than the real collection page, else None.

    Checking this on every page load (not just when a count looks wrong) is
    the whole point -- own_site_collector.py's NOTES.md finding (g) documents
    a real case where a blocked page silently produced 0 cards, which old
    logic read as "no more products, stop" instead of "this fetch failed."
    """
    if status_code is not None and status_code != 200:
        return f"non-200 status ({status_code})"
    lowered = (page_title or "").lower()
    for marker in _INTERSTITIAL_TITLE_MARKERS:
        if marker in lowered:
            return f"interstitial page title matched {marker!r} (title={page_title!r})"
    return None


# ---------------------------------------------------------------------------
# Platform: shopify_t4s (confirmed against Wonderchef, Sep 2026)
# ---------------------------------------------------------------------------
# Card container confirmed via live DOM inspection: `.t4s-main-area` appears
# exactly once per collection page and contains only the real grid's product
# cards. This matters because, exactly like own_site_collector.py's finding
# (e) (nestasia.in's search-drawer widget silently duplicating cards), this
# theme's "recently viewed" / "you may also like" carousels on the same page
# reuse the identical `.t4s-product[data-product-options]` card markup as the
# real grid -- an unscoped `[data-product-options]` query on Wonderchef's
# Cookware page picked up 40 carousel cards alongside the 30 real ones.
# Scoping to `.t4s-main-area` is a positive-scope fix (query INTO the known
# real container), not a blocklist of the carousel class, for the same
# reason own_site_collector.py chose positive scoping: there could be other
# sitewide widgets reusing this markup that haven't been found yet.
_SHOPIFY_T4S_CARD_SELECTOR = ".t4s-main-area .t4s-product[data-product-options]"

_SHOPIFY_T4S_EXTRACT_JS = """
els => els.map(el => {
    let opts = null;
    try { opts = JSON.parse(el.getAttribute('data-product-options')); } catch (e) { opts = null; }
    const addBtn = el.querySelector('.t4s-pr-addtocart, [class*="addtocart"], [class*="add-to-cart"]');
    let visibleButtonText = null;
    let buttonLooksDisabled = null;
    if (addBtn) {
        visibleButtonText = addBtn.innerText.trim() || null;
        const cls = addBtn.className || '';
        buttonLooksDisabled = addBtn.disabled === true
            || /sold-?out|disable/i.test(cls);
    }
    return {
        available: opts ? opts.available : null,
        handle: opts ? opts.handle : null,
        priceCents: opts ? opts.price : null,
        compareAtPriceCents: opts ? opts.compare_at_price : null,
        title: opts ? opts.alt : null,
        visibleButtonText: visibleButtonText,
        buttonLooksDisabled: buttonLooksDisabled,
    };
})
"""


def _stock_status_tag(available):
    if available is True:
        return "In Stock"
    if available is False:
        return "Out of Stock"
    return "Unknown"


def _actual_button_state(visible_button_text, button_looks_disabled):
    if visible_button_text:
        return visible_button_text
    if button_looks_disabled:
        return "Sold Out"  # fallback signal when no visible button text was found at all
    return "Unknown"


def _is_mismatch(stock_status_tag, actual_button_state):
    """True when the declared availability and the rendered button disagree
    -- same signal own_site_collector.py's stock_mismatch column tracks, kept
    consistent here so competitor and own-site rows are directly comparable."""
    if stock_status_tag == "Unknown" or not actual_button_state or actual_button_state == "Unknown":
        return "Unknown"
    tag_says_in_stock = stock_status_tag == "In Stock"
    button_says_in_stock = "add" in actual_button_state.lower()
    return tag_says_in_stock != button_says_in_stock


def _extract_shopify_t4s(page, base_url):
    cards = page.eval_on_selector_all(_SHOPIFY_T4S_CARD_SELECTOR, _SHOPIFY_T4S_EXTRACT_JS)
    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    rows = {}
    for card in cards:
        handle = card.get("handle")
        if not handle or not card.get("title"):
            continue
        # Canonical by handle (not the raw <a href>) because this theme's
        # card markup mixes both "/products/<handle>" and
        # "/collections/<slug>/products/<handle>" hrefs for the exact same
        # product depending on which button inside the card you read --
        # deduping on handle avoids treating those as two different SKUs.
        product_url = f"{origin}/products/{handle}"
        if product_url in rows:
            continue

        price = card["priceCents"] / 100 if card.get("priceCents") is not None else None
        compare_at_price = card["compareAtPriceCents"] / 100 if card.get("compareAtPriceCents") else None
        discount_percent = None
        if price is not None and compare_at_price and compare_at_price > 0:
            discount_percent = round((1 - price / compare_at_price) * 100)

        stock_status_tag = _stock_status_tag(card.get("available"))
        actual_button_state = _actual_button_state(card.get("visibleButtonText"), card.get("buttonLooksDisabled"))

        rows[product_url] = {
            "product_name": card["title"][:150],
            "product_url": product_url,
            "price": price,
            "compare_at_price": compare_at_price,
            "discount_percent": discount_percent,
            "stock_status_tag": stock_status_tag,
            "actual_button_state": actual_button_state,
            "stock_mismatch": _is_mismatch(stock_status_tag, actual_button_state),
        }
    return rows


def _collect_shopify_t4s(page, category_url):
    """Paginate one Shopify-t4s-theme collection page via URL query params
    (?page=N), same stop-on-empty-page / stop-on-detected-block pattern as
    own_site_collector.py's collect_subcategory(). Returns
    (found, collection_complete).
    """
    found = {}
    collection_complete = True
    hit_page_cap_with_more_available = False

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        separator = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{separator}page={page_num}" if page_num > 1 else category_url

        try:
            resp = page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"  page {page_num} load failed: {e}")
            collection_complete = False
            break

        block_reason = _detect_block(resp.status if resp else None, page.title())
        if block_reason:
            log.warning(
                f"  BLOCKED on page {page_num} (reason: {block_reason}) -- stopping here. "
                f"{len(found)} SKU(s) collected so far are PARTIAL, not the full collection."
            )
            collection_complete = False
            break

        page_rows = _extract_shopify_t4s(page, category_url)
        new_this_page = 0
        for product_url, row in page_rows.items():
            if product_url in found:
                continue
            found[product_url] = row
            new_this_page += 1

        log.info(f"  page {page_num}: {new_this_page} new SKUs (running total {len(found)})")

        if new_this_page == 0:
            log.info(f"  no new SKUs on page {page_num}, genuine end of collection")
            break

        if page_num == MAX_PAGES_PER_CATEGORY:
            # Loop is about to exit because we hit the page cap, not because
            # this page returned 0 new SKUs -- that means there is very
            # likely a page (MAX_PAGES_PER_CATEGORY + 1) with more real
            # products on it. Confirmed concretely for Wonderchef Cookware:
            # page 6 still returned 23 new SKUs, and pages 7-8 (checked
            # manually, outside the capped run) each had a full 30 cards.
            # Treating this as collection_complete=True would silently
            # under-report a real collection the exact same way
            # own_site_collector.py's NOTES.md finding (g) describes for a
            # detected block -- the cause is different (a deliberate request
            # cap, not a bot-check) but the failure mode for a downstream
            # reader of the CSV is identical, so it gets the same flag.
            hit_page_cap_with_more_available = True
            log.warning(
                f"  hit MAX_PAGES_PER_CATEGORY={MAX_PAGES_PER_CATEGORY} cap while page {page_num} still "
                f"returned {new_this_page} new SKUs -- more pages likely exist beyond the cap. "
                f"{len(found)} SKU(s) collected so far are PARTIAL, not the full collection."
            )
            break

        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))

    if hit_page_cap_with_more_available:
        collection_complete = False

    return found, collection_complete


# ---------------------------------------------------------------------------
# Platform: unbxd_nextjs (confirmed against Home Centre, Sep 2026)
# ---------------------------------------------------------------------------
# Home Centre's category listing pages (homecentre.in) are Next.js +
# Material-UI, backed by an Unbxd search/merchandising API -- a completely
# different platform from Wonderchef's Shopify theme, confirmed by live DOM
# inspection rather than assumed just because it's also a JS storefront.
#
# Two things make this platform meaningfully different from
# own_site_collector.py's and Wonderchef's collectors:
#
# 1. The initial page load embeds the full first-page listing as structured
#    JSON in `window.__NEXT_DATA__` (no DOM/class-name scraping needed for
#    page 1), but that object does NOT update when the page is paginated --
#    "NEXT PAGE" is a client-side button click, not a URL change, and it
#    fires a fresh request to `search.unbxd.io/.../category?...page=N` that
#    returns the exact same JSON shape (`{numberOfProducts, start,
#    products: [...]}`) as the embedded page-1 data. So page 1 is read from
#    __NEXT_DATA__, and every page after it is read by clicking "NEXT PAGE"
#    and intercepting that network response -- confirmed live (Cookware:
#    259 declared total, 48/page, 6 pages, last page 19, "NEXT PAGE" button
#    correctly disables itself after page 6).
#
# 2. Critically, that same live network request carries
#    `filter=inStock:"1"` -- Home Centre's own category-browse query filters
#    out-of-stock SKUs out of the listing entirely, and the grid card itself
#    has no add-to-cart button at all (only a wishlist heart icon; adding to
#    cart happens on the product detail page). This means, unlike
#    nestasia.in and Wonderchef, there is no on-grid "declared tag vs.
#    rendered button" pair to compare for a stock-mismatch check -- see
#    NOTES.md finding (i) for why "0 mismatches" here is NOT a comparable
#    result to nestasia.in's confirmed bug, and stock_mismatch is recorded
#    as "N/A" rather than True/False for this platform.
_UNBXD_NEXTJS_NEXT_DATA_JS = """
() => {
    try {
        return window.__NEXT_DATA__.props.initialState.unbxdListingPageReducer.data.response;
    } catch (e) { return null; }
}
"""


def _unbxd_product_to_row(product, origin):
    price = product.get("price")
    compare_at_price = product.get("wasPrice")
    discount_percent = product.get("percentageDiscount") or None
    in_stock = product.get("inStock")
    if in_stock == 1:
        stock_status_tag = "In Stock"
    elif in_stock == 0:
        stock_status_tag = "Out of Stock"
    else:
        stock_status_tag = "Unknown"

    raw_url = product.get("productUrl") or ""
    product_url = f"{origin}{raw_url}" if raw_url.startswith("/") else raw_url

    return {
        "product_name": (product.get("name") or "")[:150],
        "product_url": product_url,
        "price": price,
        "compare_at_price": compare_at_price,
        "discount_percent": discount_percent,
        "stock_status_tag": stock_status_tag,
        # No add-to-cart control exists on this platform's listing grid, and
        # the listing query itself filters to in-stock SKUs only -- there is
        # nothing on this page to cross-check the declared tag against.
        "actual_button_state": "N/A (no add-to-cart control on listing grid)",
        "stock_mismatch": "N/A (category browse API filters to in-stock only -- see NOTES.md finding (i))",
    }


def _collect_unbxd_nextjs(page, category_url):
    """Load page 1 from __NEXT_DATA__, then click "NEXT PAGE" and intercept
    the resulting Unbxd API response for every page after that. Returns
    (found, collection_complete).

    collection_complete is determined by comparing the running SKU count
    against the API's own declared `numberOfProducts` total -- a stronger,
    non-heuristic completeness check than own_site_collector.py's and
    Wonderchef's "stop on 0-new-page" guess, made possible because this
    platform actually tells us the true total upfront. Still treated as
    False on any detected block, any page-cap exhaustion before reaching
    that declared total, OR a 0-new page that still falls short of the
    declared total (which would indicate a real bug, not exhaustion).
    """
    origin = f"{urlparse(category_url).scheme}://{urlparse(category_url).netloc}"
    found = {}

    try:
        resp = page.goto(category_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
    except Exception as e:
        log.warning(f"  initial page load failed: {e}")
        return {}, False

    block_reason = _detect_block(resp.status if resp else None, page.title())
    if block_reason:
        log.warning(f"  BLOCKED on initial page load (reason: {block_reason}) -- stopping, 0 SKUs collected.")
        return {}, False

    try:
        r = page.evaluate(_UNBXD_NEXTJS_NEXT_DATA_JS)
    except Exception as e:
        log.warning(f"  could not read __NEXT_DATA__: {e}")
        return {}, False

    if not r or "products" not in r:
        log.warning("  __NEXT_DATA__ did not contain the expected product-listing shape -- "
                     "treating as a changed page structure / block, not a genuine empty category.")
        return {}, False

    number_of_products = r.get("numberOfProducts")
    for product in r["products"]:
        row = _unbxd_product_to_row(product, origin)
        found[row["product_url"]] = row
    log.info(f"  page 1: {len(found)} SKUs (declared category total: {number_of_products})")

    collection_complete = True
    page_num = 2
    while number_of_products is not None and len(found) < number_of_products:
        if page_num > MAX_PAGES_PER_CATEGORY:
            log.warning(
                f"  hit MAX_PAGES_PER_CATEGORY={MAX_PAGES_PER_CATEGORY} cap with {len(found)} of declared "
                f"{number_of_products} SKUs collected -- PARTIAL, not a full collection."
            )
            collection_complete = False
            break

        next_buttons = page.locator("button", has_text="NEXT PAGE")
        if next_buttons.count() == 0 or not next_buttons.first.is_enabled():
            log.warning(
                f"  \"NEXT PAGE\" button unavailable/disabled before page {page_num}, but only "
                f"{len(found)} of declared {number_of_products} SKUs collected -- this is NOT the "
                f"expected genuine-end signal (that requires the counts to match), so treating as PARTIAL."
            )
            collection_complete = False
            break

        try:
            with page.expect_response(lambda r: "search.unbxd.io" in r.url, timeout=PAGE_LOAD_TIMEOUT_MS) as resp_info:
                next_buttons.first.click()
            api_resp = resp_info.value
        except Exception as e:
            log.warning(f"  page {page_num} click/response failed: {e} -- stopping, PARTIAL.")
            collection_complete = False
            break

        block_reason = _detect_block(api_resp.status, None)
        if block_reason:
            log.warning(f"  BLOCKED on page {page_num} API call (reason: {block_reason}) -- stopping, PARTIAL.")
            collection_complete = False
            break

        try:
            body = api_resp.json()
            r = body.get("response") or {}
        except Exception as e:
            log.warning(f"  page {page_num} API response was not parseable JSON: {e} -- stopping, PARTIAL.")
            collection_complete = False
            break

        if "products" not in r:
            log.warning(f"  page {page_num} API response missing expected 'products' key -- stopping, PARTIAL.")
            collection_complete = False
            break

        new_this_page = 0
        for product in r["products"]:
            row = _unbxd_product_to_row(product, origin)
            if row["product_url"] in found:
                continue
            found[row["product_url"]] = row
            new_this_page += 1

        log.info(f"  page {page_num}: {new_this_page} new SKUs "
                 f"(running total {len(found)} of declared {number_of_products})")

        if new_this_page == 0:
            log.warning(
                f"  page {page_num} returned 0 new SKUs but running total ({len(found)}) is still short of "
                f"the declared total ({number_of_products}) -- stopping to avoid an infinite loop, but this "
                f"is a genuine discrepancy, not exhaustion. PARTIAL."
            )
            collection_complete = False
            break

        page_num += 1
        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))

    return found, collection_complete


# ---------------------------------------------------------------------------
# Platform: shopify_hyper_sections (confirmed against Milton, Sep 2026)
# ---------------------------------------------------------------------------
# Milton (milton.in) is Shopify, but a third distinct theme in this project
# (FoxTheme.settings.themeName == "Hyper") -- confirmed by live inspection to
# use neither Wonderchef's t4s markup nor Home Centre's Next.js/Unbxd stack.
# Recon (see NOTES.md finding (j)) ruled out a plain `?page=N` full-page nav
# (it silently re-served page 1) and found the real mechanism is Shopify's
# documented Section Rendering API: the theme's "Show more" button's `action`
# attribute points at `<collection_url>?filter.v.availability=N&page=P&section_id=<id>`,
# which returns just that section's HTML fragment. `section_id` is read once
# from page 1's DOM and reused for every later page -- no click, no
# network-interception timing needed (page.request.get() directly).
#
# Two things discovered only once real pagination + stock inspection was
# attempted (not visible from recon alone), both load-bearing for this build:
#
# 1. Milton's collection page redirects itself (client-side, via history
#    API -- confirmed NOT a server redirect by inspecting the raw response
#    chain) from a bare URL to `?filter.v.availability=1` by default -- the
#    same "hide out-of-stock by default" pattern as Home Centre (i). Unlike
#    Home Centre, though, Milton's theme also serves a working
#    `filter.v.availability=0` view (out-of-stock-only) on request. Since the
#    whole point of testing Milton is finding out whether nestasia.in's
#    stock-display bug exists elsewhere, collecting ONLY the default
#    (filter=1) view would recreate Home Centre's blind spot -- it would
#    structurally exclude every SKU where a mismatch could exist. So this
#    collector walks BOTH filter values and merges them.
# 2. A product can appear under BOTH filter values (confirmed: 19 of 96
#    Cookware SKUs did) -- these are multi-variant products where some
#    variants are in stock and some aren't. Their `stock_status_tag` is
#    recorded as "Mixed availability" rather than forced into a boolean,
#    since neither "In Stock" nor "Out of Stock" alone would be honest.
#
# Card markup uses human-readable BEM classes (`.product-card`,
# `.product-card-add-btn`), not auto-generated hashes -- read directly, no
# JSON data-attribute like Wonderchef's `data-product-options` exists here.
_MILTON_SECTION_ID_JS = """
() => {
    const el = document.querySelector('[id^="shopify-section-"][id*="collection_product_grid"]');
    return el ? el.id.replace('shopify-section-', '') : null;
}
"""

_MILTON_CARD_EXTRACT_JS = """
els => els.map(el => {
    const a = el.querySelector('a[href*="/products/"]');
    const nameEl = el.querySelector('.product-card__title');
    const salePriceEl = el.querySelector('.f-price-item--sale');
    const strikethroughEl = el.querySelector('.f-price__sale s .f-price-item--regular');
    const addBtn = el.querySelector('.product-card-add-btn');
    let addBtnVisible = false;
    if (addBtn) {
        const cs = getComputedStyle(addBtn);
        addBtnVisible = cs.display !== 'none' && cs.visibility !== 'hidden';
    }
    // Positively check the specific sold-out badge class AND its actual
    // computed visibility -- a first draft of this extractor used a blind
    // regex over the whole card's innerText for "sold out", which is the
    // exact fragile-coincidental-signal mistake own_site_collector.py's
    // _detect_amazon_block comment already warns about (a hidden quick-view
    // modal or off-screen tooltip text could match without a shopper ever
    // seeing it). This card's own theme literally has a `.f-badge--soldout`
    // class for this, confirmed via live inspection -- use it directly.
    const soldOutBadge = el.querySelector('.f-badge--soldout');
    let soldOutBadgeVisible = false;
    if (soldOutBadge) {
        const cs = getComputedStyle(soldOutBadge);
        soldOutBadgeVisible = cs.display !== 'none' && cs.visibility !== 'hidden';
    }
    return {
        url: a ? a.getAttribute('href').split('?')[0] : null,
        name: nameEl ? nameEl.innerText.trim() : null,
        salePriceText: salePriceEl ? salePriceEl.innerText.trim() : null,
        regularPriceText: strikethroughEl ? strikethroughEl.innerText.trim() : null,
        addBtnText: addBtn ? addBtn.innerText.trim() : null,
        addBtnPresent: !!addBtn,
        addBtnVisible: addBtnVisible,
        soldOutBadgeVisible: soldOutBadgeVisible,
    };
})
"""


def _parse_milton_price(text):
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    return float(digits) if digits else None


def _milton_actual_button_state(add_btn_text, add_btn_visible, sold_out_badge_visible):
    """Both signals are reported together, deliberately not collapsed into
    one -- the real Milton bug this session confirmed is exactly a card
    where BOTH a visible "Sold out" badge AND a visible, active "Add" quick-
    add control render at once (verified live: getComputedStyle showed
    display:flex/visible on the badge and display:inline on the Add link
    simultaneously, on the same card, for 6 confirmed-sold-out products).
    Collapsing that into a single value (an earlier draft of this function
    did) hides the actual bug rather than reporting it."""
    parts = []
    if add_btn_visible:
        parts.append(add_btn_text or "Add (no text captured)")
    if sold_out_badge_visible:
        parts.append("Sold Out badge visible")
    if not parts:
        return "Unknown (no add-to-cart control or sold-out badge visible on this card)"
    return " + ".join(parts)


def _milton_is_mismatch(stock_status_tag, actual_button_state):
    """Same declared-tag-vs-rendered-button comparison as nestasia.in's
    stock_mismatch column (own_site_collector.py's _is_mismatch) and
    Wonderchef's (_is_mismatch above) -- kept consistent so results are
    directly comparable across all three platforms that actually support
    this check. "Mixed availability" products are deliberately excluded from
    a True/False verdict (see the platform docstring above) since the
    button's default-variant rendering isn't dishonest in the same way a
    fully-out-of-stock product showing "Add" is.

    Only an AFFIRMATIVE contradiction counts as a mismatch -- not merely the
    absence of a quick-add control. A first test run of this collector
    flagged 14 false positives here: multi-variant products whose cards
    render a "View details" link instead of a one-click "Add" (Shopify can't
    add-to-cart without a variant selection), which this function's first
    draft treated as an implicit "not in stock" signal. Verified directly
    against one flagged product's real product page
    (products/hexatech-4-pcs-set-procook-by-milton): "Add To Cart", enabled
    -- genuinely in stock, no bug. `actual_button_state` starting with
    "Unknown" now returns "Unknown" here too, rather than being silently
    treated as a negative signal."""
    if stock_status_tag == "Mixed availability (some variants in stock, some out of stock)":
        return "N/A (mixed-variant product; see NOTES.md finding (k))"
    if actual_button_state.startswith("Unknown"):
        return "Unknown (no add-to-cart control on this card -- likely a multi-variant product " \
               "requiring PDP selection, not evidence of a mismatch either way)"
    add_button_active = actual_button_state.startswith("Add")
    sold_out_badge_shown = "Sold Out badge visible" in actual_button_state
    if stock_status_tag == "In Stock (per site's default in-stock filter)":
        return sold_out_badge_shown
    if stock_status_tag == "Out of Stock (per site's out-of-stock filter)":
        return add_button_active
    return "Unknown"


def _milton_card_to_row(card, origin):
    if card.get("url", "").startswith("/"):
        product_url = f"{origin}{card['url']}"
    else:
        product_url = card.get("url")

    price = _parse_milton_price(card.get("salePriceText"))
    compare_at_price = _parse_milton_price(card.get("regularPriceText"))
    discount_percent = None
    if price is not None and compare_at_price and compare_at_price > price:
        discount_percent = round((1 - price / compare_at_price) * 100)

    actual_button_state = _milton_actual_button_state(
        card.get("addBtnText"), card.get("addBtnVisible"), card.get("soldOutBadgeVisible")
    )

    return {
        "product_name": (card.get("name") or "")[:150],
        "product_url": product_url,
        "price": price,
        "compare_at_price": compare_at_price,
        "discount_percent": discount_percent,
        "actual_button_state": actual_button_state,
        # stock_status_tag and stock_mismatch are filled in by the caller,
        # which knows which filter pass(es) this product was found under --
        # that information isn't available from a single card alone.
    }


def _walk_milton_filter(page, frag_page, category_url, section_id, filter_value):
    """Paginate one filter.v.availability value's results via the Section
    Rendering API. Returns (found, collection_complete).

    Per instructions, collection_complete requires BOTH end-signals to
    agree -- the "load more" button disappearing from the page AND the
    subsequent page independently coming back empty -- not just the first
    one observed. If the button disappears but the next page still returns
    new SKUs (the two signals disagreeing), that is treated as NOT complete
    rather than trusting whichever signal happened to fire, since that
    disagreement is itself evidence something about this run wasn't clean.
    """
    found = {}
    button_seen_gone_at_page = None

    page_num = 1
    while True:
        if page_num == 1:
            url = f"{category_url}?filter.v.availability={filter_value}"
            try:
                resp = page.goto(url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            except Exception as e:
                log.warning(f"  [filter={filter_value}] page 1 load failed: {e}")
                return {}, False
            block_reason = _detect_block(resp.status if resp else None, page.title())
            if block_reason:
                log.warning(f"  [filter={filter_value}] BLOCKED on page 1 (reason: {block_reason}) -- 0 SKUs, PARTIAL.")
                return {}, False
            target = page
        else:
            url = f"{category_url}?filter.v.availability={filter_value}&page={page_num}&section_id={section_id}"
            try:
                api_resp = page.request.get(url, timeout=PAGE_LOAD_TIMEOUT_MS)
            except Exception as e:
                log.warning(f"  [filter={filter_value}] page {page_num} fetch failed: {e} -- stopping, PARTIAL.")
                return found, False
            block_reason = _detect_block(api_resp.status, None)
            if block_reason:
                log.warning(f"  [filter={filter_value}] BLOCKED on page {page_num} (reason: {block_reason}) -- stopping, PARTIAL.")
                return found, False
            frag_page.set_content(api_resp.text())
            target = frag_page

        cards = target.eval_on_selector_all(".product-card", _MILTON_CARD_EXTRACT_JS)
        origin = f"{urlparse(category_url).scheme}://{urlparse(category_url).netloc}"

        new_this_page = 0
        for card in cards:
            if not card.get("url") or not card.get("name"):
                continue
            row = _milton_card_to_row(card, origin)
            if row["product_url"] in found:
                continue
            found[row["product_url"]] = row
            new_this_page += 1

        has_button = target.evaluate('!!document.querySelector(\'button[is="load-more-button"]\')')
        log.info(f"  [filter={filter_value}] page {page_num}: {new_this_page} new SKUs "
                 f"(running total {len(found)}), returned={len(cards)}, has_next_button={has_button}")

        if len(cards) == 0:
            if button_seen_gone_at_page is not None:
                log.info(f"  [filter={filter_value}] page {page_num} confirmed empty after the \"load more\" "
                         f"button disappeared on page {button_seen_gone_at_page} -- BOTH end-signals agree, genuine end.")
                return found, True
            log.warning(f"  [filter={filter_value}] page {page_num} came back empty but the \"load more\" button "
                        f"was never observed disappearing first -- only one signal, not the required two. PARTIAL.")
            return found, False

        if not has_button:
            if button_seen_gone_at_page is None:
                button_seen_gone_at_page = page_num
                log.info(f"  [filter={filter_value}] \"load more\" button gone after page {page_num} -- "
                         f"fetching one more page to confirm with the second signal before trusting this.")
            else:
                log.warning(f"  [filter={filter_value}] button was already gone after page {button_seen_gone_at_page}, "
                            f"but page {page_num} still returned {new_this_page} new SKUs -- the two end-signals "
                            f"DISAGREE. Not trusting either one alone. PARTIAL.")
                return found, False

        if page_num >= MAX_PAGES_PER_CATEGORY:
            log.warning(f"  [filter={filter_value}] hit MAX_PAGES_PER_CATEGORY={MAX_PAGES_PER_CATEGORY} cap "
                        f"with {len(found)} SKUs and no confirmed genuine end yet -- PARTIAL.")
            return found, False

        page_num += 1
        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))


def _collect_shopify_hyper_sections(page, category_url):
    """Collects Milton's Cookware (or similar) collection by walking BOTH
    filter.v.availability values (1 = in stock, 0 = out of stock) and
    merging -- see the platform docstring above for why collecting only the
    default in-stock view would make stock-mismatch detection vacuous, the
    same trap Home Centre's architecture fell into structurally.
    """
    try:
        resp = page.goto(category_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except Exception as e:
        log.warning(f"  initial page load failed: {e}")
        return {}, False

    block_reason = _detect_block(resp.status if resp else None, page.title())
    if block_reason:
        log.warning(f"  BLOCKED on initial page load (reason: {block_reason}) -- stopping, 0 SKUs collected.")
        return {}, False

    section_id = page.evaluate(_MILTON_SECTION_ID_JS)
    if not section_id:
        log.warning("  could not find the collection product-grid section id in the DOM -- "
                    "treating as a changed page structure, not a genuine empty category.")
        return {}, False
    log.info(f"  section_id resolved: {section_id}")

    frag_page = page.context.new_page()
    try:
        in_stock_found, in_stock_complete = _walk_milton_filter(page, frag_page, category_url, section_id, 1)
        out_of_stock_found, out_of_stock_complete = _walk_milton_filter(page, frag_page, category_url, section_id, 0)
    finally:
        frag_page.close()

    merged = {}
    for url, row in in_stock_found.items():
        row["stock_status_tag"] = "In Stock (per site's default in-stock filter)"
        merged[url] = row
    for url, row in out_of_stock_found.items():
        if url in merged:
            merged[url]["stock_status_tag"] = "Mixed availability (some variants in stock, some out of stock)"
        else:
            row["stock_status_tag"] = "Out of Stock (per site's out-of-stock filter)"
            merged[url] = row

    for row in merged.values():
        row["stock_mismatch"] = _milton_is_mismatch(row["stock_status_tag"], row["actual_button_state"])

    collection_complete = in_stock_complete and out_of_stock_complete
    log.info(f"  merged: {len(merged)} unique SKUs (in-stock pass: {len(in_stock_found)}, "
             f"out-of-stock pass: {len(out_of_stock_found)}, overlap: "
             f"{len(set(in_stock_found) & set(out_of_stock_found))}) -- "
             f"in-stock pass complete={in_stock_complete}, out-of-stock pass complete={out_of_stock_complete}")

    return merged, collection_complete


# ---------------------------------------------------------------------------
# Platform: magento_luma (confirmed against Prestige/TTK Prestige, Sep 2026)
# ---------------------------------------------------------------------------
# Prestige (shop.ttkprestige.com -- a separate subdomain from the main
# ttkprestige.com site, found only by following the real "Cookware" nav link,
# not guessed) is Magento (Luma theme + the Amasty "Shop By" layered
# navigation extension) -- a fourth distinct platform in this project, none
# of Wonderchef's, Home Centre's, or Milton's selectors or pagination
# mechanics apply. Confirmed by live inspection: `window.checkout` and a
# `BASE_URL` global (Magento frontend markers), `mage-cache-storage` in
# localStorage, and `.products-grid > .product-items > .product-item`
# markup, none of which match any prior platform.
#
# Card container confirmed unique and uncontaminated: `#amasty-shopby-
# product-list .product-item` count matched the unscoped `.product-item`
# count exactly on a live check (16 == 16) -- unlike nestasia.in's search-
# drawer or Wonderchef's carousel, there is no sitewide widget reusing this
# markup elsewhere on the page here, so no positive-scoping fix was needed
# beyond simply confirming that first.
#
# Pagination: plain `?p=N` query param via direct `page.goto()` -- the
# simplest mechanism of the four platforms so far, confirmed by observing
# genuinely different product links between `?p=1` and `?p=2` (unlike
# Milton's broken `?page=N`, which silently re-served page 1).
#
# Availability filter check (done explicitly, not assumed from Wonderchef's
# precedent): the bare category URL never redirects to a filtered variant
# (unlike Home Centre and Milton's silent client-side redirects), explicit
# query-param guesses had no effect, no availability checkbox/radio exists
# in the DOM, and reading all facet labels in the Amasty filter sidebar
# (Price, Base, Capacity, Country Of Origin, Includes Lid, Material, Power,
# Product Sub Type, Product Type, Size, Type, Store Code) found no
# availability/stock option at all. **No default availability filter
# exists** -- confirmed the same rigorous way as Wonderchef's absence was
# confirmed, not inferred from "it's a different platform than Home Centre
# and Milton."
#
# Stock signal, confirmed by scanning the full 321-product Cookware category
# and spot-checking against real product pages: Magento's Luma catalog-list
# template only renders a `.stock` block on the GRID card when a product is
# NOT saleable -- saleable products get no `.stock` element on the grid at
# all (confirmed: zero `.stock` elements across the first 11 pages, all of
# which were independently spot-checked as "In stock" on their own product
# pages). Out-of-stock products get `.stock.unavailable` with the text
# "OUT OF STOCK", and correctly have no add-to-cart link rendered on the
# grid either -- every one of the 133 unavailable-marked cards found in this
# scan agreed cleanly (no add link, "OUT OF STOCK" badge). A separate,
# common false-positive was checked and ruled out first: many *in-stock*
# configurable products (need a size/variant picked before "Add to Cart"
# can render) also show no add-to-cart link on the grid -- this is NOT a
# stock signal, confirmed by spot-checking 3 such cards' real product pages
# (all "In stock", functioning Add to Cart) -- this is the exact same
# false-positive shape Milton's first draft had, checked for and avoided
# here from the start rather than rediscovered.
_MAGENTO_CARD_EXTRACT_JS = """
els => els.map(el => {
    const linkEl = el.querySelector('a.product-item-link');
    const addLink = el.querySelector('a.action.tocart');
    const stockEl = el.querySelector('.stock');
    let addVisible = false, addText = null;
    if (addLink) {
        const cs = getComputedStyle(addLink);
        addVisible = cs.display !== 'none' && cs.visibility !== 'hidden';
        addText = addLink.innerText.trim();
    }
    let stockVisible = false, stockText = null, stockClass = null;
    if (stockEl) {
        const cs = getComputedStyle(stockEl);
        stockVisible = cs.display !== 'none' && cs.visibility !== 'hidden';
        stockText = stockEl.innerText.trim();
        stockClass = stockEl.className;
    }
    const priceEl = el.querySelector('.product_view-price-top .price-wrapper .price');
    const oldPriceEl = el.querySelector('.old-price .price-wrapper .price');
    return {
        url: linkEl ? linkEl.href.split('?')[0] : null,
        name: linkEl ? linkEl.innerText.trim() : null,
        priceText: priceEl ? priceEl.innerText.trim() : null,
        oldPriceText: oldPriceEl ? oldPriceEl.innerText.trim() : null,
        addVisible: addVisible,
        addText: addText,
        stockVisible: stockVisible,
        stockText: stockText,
        stockClass: stockClass,
    };
})
"""


def _parse_magento_price(text):
    if not text:
        return None
    digits = "".join(ch for ch in text if ch.isdigit() or ch == ".")
    return float(digits) if digits else None


def _magento_stock_status_tag(stock_visible, stock_class):
    if stock_visible and stock_class and "unavailable" in stock_class:
        return "Out of Stock"
    if stock_visible and stock_class and "available" in stock_class:
        return "In Stock (explicit .stock.available badge)"
    # No .stock element at all -- confirmed via spot-checks that this means
    # saleable/in-stock on this theme, not an absence of data.
    return "In Stock (inferred -- grid omits the stock block for saleable items)"


def _magento_actual_button_state(add_visible, add_text):
    if add_visible:
        return add_text or "Add to Cart (no text captured)"
    return "No add-to-cart control visible on grid"


def _magento_is_mismatch(stock_status_tag, actual_button_state):
    """Same principle as Milton's fixed logic: only an affirmative
    contradiction counts, never the mere absence of a control -- configurable
    products correctly show no quick-add link while genuinely in stock, and
    that must not be misread as a negative signal (see the platform
    docstring above)."""
    add_active = actual_button_state.lower().startswith("add")
    if actual_button_state == "No add-to-cart control visible on grid" and stock_status_tag.startswith("In Stock"):
        return "Unknown (no add-to-cart control on this card -- likely a configurable product " \
               "requiring PDP variant selection, not evidence of a mismatch either way)"
    if stock_status_tag == "Out of Stock":
        return add_active
    if stock_status_tag.startswith("In Stock"):
        return not add_active and actual_button_state != "No add-to-cart control visible on grid"
    return "Unknown"


def _magento_card_to_row(card, origin):
    url = card.get("url")
    if url and url.startswith("/"):
        url = f"{origin}{url}"

    price = _parse_magento_price(card.get("priceText"))
    compare_at_price = _parse_magento_price(card.get("oldPriceText"))
    discount_percent = None
    if price is not None and compare_at_price and compare_at_price > price:
        discount_percent = round((1 - price / compare_at_price) * 100)

    stock_status_tag = _magento_stock_status_tag(card.get("stockVisible"), card.get("stockClass"))
    actual_button_state = _magento_actual_button_state(card.get("addVisible"), card.get("addText"))

    return {
        "product_name": (card.get("name") or "")[:150],
        "product_url": url,
        "price": price,
        "compare_at_price": compare_at_price,
        "discount_percent": discount_percent,
        "stock_status_tag": stock_status_tag,
        "actual_button_state": actual_button_state,
        "stock_mismatch": _magento_is_mismatch(stock_status_tag, actual_button_state),
    }


def _magento_declared_total(page):
    """Reads the toolbar's "Items X-Y of Z" count -- an authoritative
    declared total, the same kind of independent completeness signal Home
    Centre's `numberOfProducts` provided. Returns None if not found (e.g. a
    genuinely empty category), which callers treat as "no declared-total
    signal available" rather than zero."""
    text = page.evaluate("(document.querySelector('.toolbar-amount') || {}).innerText || null")
    if not text:
        return None
    import re
    m = re.search(r"of\s+([\d,]+)", text)
    return int(m.group(1).replace(",", "")) if m else None


def _collect_magento_luma(page, category_url):
    """Paginate via plain `?p=N` navigation. collection_complete requires
    the declared toolbar total to be reached AND the "next" pagination link
    to have disappeared AND the page past the last one to independently
    come back empty -- three-way corroboration, deliberately more than
    Milton's two-signal minimum, since this platform offers a declared total
    that the other page-cap-bug platforms did not.
    """
    found = {}
    declared_total = None
    origin = f"{urlparse(category_url).scheme}://{urlparse(category_url).netloc}"

    page_num = 1
    next_link_gone_at_page = None
    while True:
        separator = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{separator}p={page_num}" if page_num > 1 else category_url

        try:
            resp = page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(1800)
        except Exception as e:
            log.warning(f"  page {page_num} load failed: {e}")
            return found, False

        block_reason = _detect_block(resp.status if resp else None, page.title())
        if block_reason:
            log.warning(f"  BLOCKED on page {page_num} (reason: {block_reason}) -- stopping, PARTIAL.")
            return found, False

        if page_num == 1:
            declared_total = _magento_declared_total(page)
            log.info(f"  declared category total: {declared_total}")

        cards = page.eval_on_selector_all("#amasty-shopby-product-list .product-item", _MAGENTO_CARD_EXTRACT_JS)
        new_this_page = 0
        for card in cards:
            if not card.get("url") or not card.get("name"):
                continue
            row = _magento_card_to_row(card, origin)
            if row["product_url"] in found:
                continue
            found[row["product_url"]] = row
            new_this_page += 1

        has_next = page.evaluate("document.querySelectorAll('a.action.next').length > 0")
        log.info(f"  page {page_num}: {new_this_page} new SKUs (running total {len(found)} of declared "
                 f"{declared_total}), returned={len(cards)}, has_next_link={has_next}")

        if len(cards) == 0:
            if next_link_gone_at_page is not None:
                genuine_end = declared_total is None or len(found) == declared_total
                if genuine_end:
                    log.info(f"  page {page_num} confirmed empty after the next-page link disappeared on page "
                             f"{next_link_gone_at_page}, AND running total matches the declared total -- "
                             f"all signals agree, genuine end.")
                    return found, True
                log.warning(f"  page {page_num} is empty and the next-link was already gone, but running total "
                            f"({len(found)}) does NOT match the declared total ({declared_total}) -- "
                            f"signals disagree. PARTIAL.")
                return found, False
            log.warning(f"  page {page_num} came back empty but the next-page link was never observed "
                        f"disappearing first -- only one signal, not the required agreement. PARTIAL.")
            return found, False

        if not has_next:
            if next_link_gone_at_page is None:
                next_link_gone_at_page = page_num
                log.info(f"  next-page link gone after page {page_num} -- fetching one more page to confirm "
                         f"with the second signal before trusting this.")
            else:
                log.warning(f"  next-link was already gone after page {next_link_gone_at_page}, but page "
                            f"{page_num} still returned {new_this_page} new SKUs -- signals DISAGREE. PARTIAL.")
                return found, False

        if page_num >= MAX_PAGES_PER_CATEGORY:
            log.warning(f"  hit MAX_PAGES_PER_CATEGORY={MAX_PAGES_PER_CATEGORY} cap with {len(found)} SKUs "
                        f"(declared {declared_total}) and no confirmed genuine end yet -- PARTIAL.")
            return found, False

        page_num += 1
        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))


# ---------------------------------------------------------------------------
# Platform: shopify_borosil_revamp (confirmed against Borosil/myborosil.com, Sep 2026)
# ---------------------------------------------------------------------------
# Borosil (myborosil.com) is Shopify, but yet another distinct custom theme
# (window.Shopify.theme.name == "borosil-revamp/go-live-optimized-030625")
# -- a fifth distinct platform in this project, confirmed by live inspection
# rather than assumed from "it's Shopify" (Wonderchef's t4s theme and
# Milton's Hyper theme were both also "just Shopify" and shared none of
# Borosil's markup).
#
# Card scoping, confirmed the hard way: the obvious `[data-product-id]`
# selector is a TRAP here -- it matches hidden per-variant `<input
# data-product-id=...>` radio elements (used for color/size swatches), NOT
# one-per-card. An unscoped query on this attribute returned 242 hits for a
# category with only 68 real products. Worse, the very first
# `a[href*='/products/']` on the page was the site's own header mega-menu
# link, not a product card at all -- the same class of unscoped-selector
# trap nestasia.in's search drawer and Wonderchef's carousel caused
# earlier. The real, positively-scoped, confirmed-unique card element is
# the theme's own custom web component: `.product-grid borosil-product-card`
# (id attribute = the real Shopify product id).
#
# Each card embeds the FULL Shopify product JSON inline
# (`<script type="application/json" class="variant-data">`), including
# `available`, `price`, `compare_at_price`, and a `variants` array -- richer
# and more reliable than reading scattered data-* attributes off the DOM,
# the way Wonderchef's `data-product-options` was. No JS-context evaluation
# of computed availability is needed for the declared signal; it's read
# directly from this JSON.
#
# Pagination: plain `?page=N` via direct `page.goto()`, confirmed working
# (genuinely different product ids between pages, unlike Milton's broken
# `?page=N`). collection_complete requires the SAME two-independent-signal
# agreement Milton's and Prestige's collectors use (next-page link gone AND
# the following page independently coming back empty), built in from the
# start here rather than the older single-signal "stop on 0-new" pattern --
# confirmed live: the next-page link disappeared after page 3, and page 4
# independently came back with 0 cards, for a real total of 68 SKUs.
#
# Availability filter, checked explicitly, not assumed from Wonderchef/
# Prestige's precedent: the collection page has a genuine "Exclude Out Of
# Stock" checkbox (the same kind of tell Milton's "X of Y" count string
# was), but it is UNCHECKED by default -- confirmed both from its DOM state
# (`input.checked === false` on a fresh load) and behaviorally (6 of the 68
# products in the default, unfiltered listing are declared unavailable, so
# a default filter would have hidden them and it doesn't). No dual-pass
# walk is needed here, unlike Home Centre and Milton.
#
# Stock-mismatch check, ground-truth verified before writing this comment,
# not after: the add-to-cart button carries `aria-disabled` plus a
# `.sold-out-message` span, checked via `getComputedStyle` (never a text
# regex) for genuine visibility. Of the 6 declared-unavailable (single-
# variant, `available: false` at both the product and its one variant --
# no multi-variant ambiguity) products, 3 show `aria-disabled="true"`
# (correct) and 3 show `aria-disabled="false"` with the sold-out span not
# visible (an active-looking button on a genuinely, unambiguously
# unavailable product). Spot-checked 2 of those 3 directly against their
# real product pages, in fresh, isolated browser contexts (no shared
# cart/session state that could explain an "Added, go to bag" label as
# stale state): both show a non-disabled add-to-cart button AND a visible
# "Get Notified when this product comes In [stock]" restock-alert widget on
# the same page -- the same class of bug confirmed on Milton (l): a
# genuinely out-of-stock product with an active-looking add-to-cart control
# nothing on the page disables.
_BOROSIL_CARD_SELECTOR = ".product-grid borosil-product-card"

_BOROSIL_EXTRACT_JS = """
els => els.map(el => {
    const script = el.querySelector('script.variant-data');
    let data = null;
    try { data = JSON.parse(script.textContent); } catch (e) { data = null; }
    if (!data) return null;

    const btn = el.querySelector('button[data-sold-out-message]');
    let ariaDisabled = null;
    let soldOutVisible = false;
    if (btn) {
        ariaDisabled = btn.getAttribute('aria-disabled');
        const span = btn.querySelector('.sold-out-message');
        if (span) {
            const cs = getComputedStyle(span);
            soldOutVisible = cs.display !== 'none' && cs.visibility !== 'hidden';
        }
    }

    return {
        handle: data.handle,
        title: data.title,
        priceCents: data.price,
        compareAtPriceCents: data.compare_at_price,
        available: data.available,
        ariaDisabled: ariaDisabled,
        soldOutVisible: soldOutVisible,
    };
}).filter(x => x)
"""


def _borosil_actual_button_state(aria_disabled, sold_out_visible):
    if sold_out_visible:
        return "Sold Out (visible text on button)"
    if aria_disabled == "true":
        return "Sold Out (button aria-disabled)"
    if aria_disabled == "false":
        return "Add to Cart (button active, not disabled)"
    return "Unknown (no add-to-cart control found on card)"


def _borosil_is_mismatch(stock_status_tag, actual_button_state):
    """Same tag-vs-button principle as every other platform's stock_mismatch
    -- only an affirmative contradiction counts, never a missing signal."""
    if stock_status_tag == "Unknown" or actual_button_state.startswith("Unknown"):
        return "Unknown"
    button_says_in_stock = actual_button_state.startswith("Add to Cart")
    tag_says_in_stock = stock_status_tag == "In Stock"
    return tag_says_in_stock != button_says_in_stock


def _extract_shopify_borosil_revamp(page, base_url):
    cards = page.eval_on_selector_all(_BOROSIL_CARD_SELECTOR, _BOROSIL_EXTRACT_JS)
    origin = f"{urlparse(base_url).scheme}://{urlparse(base_url).netloc}"

    rows = {}
    for card in cards:
        handle = card.get("handle")
        if not handle or not card.get("title"):
            continue
        product_url = f"{origin}/products/{handle}"
        if product_url in rows:
            continue

        price = card["priceCents"] / 100 if card.get("priceCents") is not None else None
        compare_at_price = card["compareAtPriceCents"] / 100 if card.get("compareAtPriceCents") else None
        discount_percent = None
        if price is not None and compare_at_price and compare_at_price > price:
            discount_percent = round((1 - price / compare_at_price) * 100)

        stock_status_tag = _stock_status_tag(card.get("available"))
        actual_button_state = _borosil_actual_button_state(card.get("ariaDisabled"), card.get("soldOutVisible"))

        rows[product_url] = {
            "product_name": card["title"][:150],
            "product_url": product_url,
            "price": price,
            "compare_at_price": compare_at_price,
            "discount_percent": discount_percent,
            "stock_status_tag": stock_status_tag,
            "actual_button_state": actual_button_state,
            "stock_mismatch": _borosil_is_mismatch(stock_status_tag, actual_button_state),
        }
    return rows


def _collect_shopify_borosil_revamp(page, category_url):
    """Paginate via plain ?page=N. collection_complete requires BOTH the
    next-page link disappearing AND the following page independently
    coming back empty -- built in from the start (see the platform
    docstring above), not the older single-signal pattern.
    """
    found = {}
    next_link_gone_at_page = None

    page_num = 1
    while True:
        separator = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{separator}page={page_num}" if page_num > 1 else category_url

        try:
            resp = page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"  page {page_num} load failed: {e}")
            return found, False

        block_reason = _detect_block(resp.status if resp else None, page.title())
        if block_reason:
            log.warning(f"  BLOCKED on page {page_num} (reason: {block_reason}) -- stopping, PARTIAL.")
            return found, False

        page_rows = _extract_shopify_borosil_revamp(page, category_url)
        new_this_page = 0
        for product_url, row in page_rows.items():
            if product_url in found:
                continue
            found[product_url] = row
            new_this_page += 1

        has_next = page.evaluate(f"!!document.querySelector(\"a[href*='page={page_num + 1}']\")")
        log.info(f"  page {page_num}: {new_this_page} new SKUs (running total {len(found)}), "
                 f"returned={len(page_rows)}, has_next_link={has_next}")

        if len(page_rows) == 0:
            if next_link_gone_at_page is not None:
                log.info(f"  page {page_num} confirmed empty after the next-page link disappeared on page "
                         f"{next_link_gone_at_page} -- both end-signals agree, genuine end.")
                return found, True
            log.warning(f"  page {page_num} came back empty but the next-page link was never observed "
                        f"disappearing first -- only one signal, not the required two. PARTIAL.")
            return found, False

        if not has_next:
            if next_link_gone_at_page is None:
                next_link_gone_at_page = page_num
                log.info(f"  next-page link gone after page {page_num} -- fetching one more page to confirm "
                         f"with the second signal before trusting this.")
            else:
                log.warning(f"  next-link was already gone after page {next_link_gone_at_page}, but page "
                            f"{page_num} still returned {new_this_page} new SKUs -- signals DISAGREE. PARTIAL.")
                return found, False

        if page_num >= MAX_PAGES_PER_CATEGORY:
            log.warning(f"  hit MAX_PAGES_PER_CATEGORY={MAX_PAGES_PER_CATEGORY} cap with {len(found)} SKUs "
                        f"and no confirmed genuine end yet -- PARTIAL.")
            return found, False

        page_num += 1
        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))


PLATFORM_COLLECTORS = {
    "shopify_t4s": _collect_shopify_t4s,
    "unbxd_nextjs": _collect_unbxd_nextjs,
    "shopify_hyper_sections": _collect_shopify_hyper_sections,
    "magento_luma": _collect_magento_luma,
    "shopify_borosil_revamp": _collect_shopify_borosil_revamp,
}


def collect_category(page, category_url, platform):
    collector = PLATFORM_COLLECTORS.get(platform)
    if collector is None:
        log.error(f"  no collector implemented for platform={platform!r} -- skipping this row entirely "
                  f"rather than guessing selectors for an uninspected site.")
        return {}, False
    return collector(page, category_url)


# ---------------------------------------------------------------------------
# Output dataset handling
# ---------------------------------------------------------------------------
FIELDNAMES = [
    "brand", "category", "product_name", "product_url", "price", "compare_at_price",
    "discount_percent", "stock_status_tag", "actual_button_state", "stock_mismatch",
    "collection_complete", "source_type", "collected_at",
]


def append_catalogue(rows):
    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    if not os.path.exists(SITES_FILE):
        log.error(f"{SITES_FILE} not found.")
        return

    per_row_counts = {}
    partial_rows = set()
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

        with open(SITES_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                brand = row["brand"].strip()
                category = row["category"].strip()
                category_url = row["url"].strip()
                platform = row["platform"].strip()
                key = f"{brand} / {category}"

                log.info(f"Collecting {key} ({category_url}) [platform={platform}]...")
                found, collection_complete = collect_category(page, category_url, platform)
                status_note = "" if collection_complete else " *** PARTIAL -- blocked mid-pagination ***"
                log.info(f"  -> {len(found)} SKUs found for {key}{status_note}")

                if not collection_complete:
                    partial_rows.add(key)

                per_row_counts[key] = per_row_counts.get(key, 0) + len(found)

                for product_url, sku in found.items():
                    sku["brand"] = brand
                    sku["category"] = category
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
    print("Competitor catalogue collection summary")
    print("=" * 60)
    for key, count in per_row_counts.items():
        flag = "  *** PARTIAL -- blocked mid-pagination, NOT a full count ***" if key in partial_rows else ""
        print(f"  {key}: {count} SKUs{flag}")
    print(f"  Total SKUs collected this run: {len(all_rows)}")
    if partial_rows:
        print()
        print(f"WARNING: {len(partial_rows)} row(s) hit a block mid-pagination and are "
              f"INCOMPLETE, not a true collection count: {', '.join(sorted(partial_rows))}")
    print()
    if mismatches:
        print(f"STOCK-STATE MISMATCHES FOUND: {len(mismatches)}")
        for m in mismatches:
            print(f"  - [{m['brand']} / {m['category']}] {m['product_name']}: "
                  f"tag='{m['stock_status_tag']}' vs button='{m['actual_button_state']}' -- {m['product_url']}")
    else:
        print("No stock-state mismatches found in this run.")
    print("=" * 60)


if __name__ == "__main__":
    main()
