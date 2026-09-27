"""
Third attempt at collecting Container (jars-canisters, fridge-storage-
containers) and Lunch Boxes+Bags (lunch-boxes, lunch-bags) -- see
NOTES.md finding (g) for attempts 1 and 2, both of which ran all four
collections sequentially in ONE continuous Playwright browser context and
saw a block on the first collection spread to the other three on their
very first request each. That pattern (spread to a URL never touched yet
this session) looks like session-level state -- cookies, or browser
fingerprint continuity within one context -- not purely per-URL or per-IP
blocking.

This script tests that theory directly: every one of the four collections
runs in its OWN fresh `browser.new_context()`, created and torn down
per-collection, nothing shared between them except the underlying browser
process. Reuses own_site_collector.py's collect_subcategory() unchanged --
same page-cap-vs-genuine-end distinction (MAX_PAGES_PER_SUBCATEGORY=12,
collection_complete requires a real end signal), same 9s pacing (not
doubled -- doubling didn't help attempt 2).

Process, per instructions:
  1. Single, minimal recon: one page load of jars-canisters, its own fresh
     context. If blocked, STOP immediately -- don't touch the other three.
  2. If recon is clean, proceed one collection at a time, each in a fresh
     context. If a collection gets blocked, that collection alone is
     marked collection_complete=False -- the NEXT collection still gets
     its own fresh context and is attempted regardless, which is the
     actual hypothesis test (does isolation contain the block, or does it
     still spread to a never-touched URL in a brand-new context?).

Usage:
    python nestasia_isolation_recovery_attempt3.py
Appends any successfully collected rows to nestasia_catalogue.csv via
own_site_collector.py's own append_catalogue(), same non-incremental
snapshot convention as every other run.
"""
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

import own_site_collector as osc

COLLECTIONS = [
    ("Container", "https://nestasia.in/collections/jars-canisters"),
    ("Container", "https://nestasia.in/collections/fridge-storage-containers"),
    ("Lunch Boxes+Bags", "https://nestasia.in/collections/lunch-boxes"),
    ("Lunch Boxes+Bags", "https://nestasia.in/collections/lunch-bags"),
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _fresh_context_and_page(browser):
    context = browser.new_context(user_agent=USER_AGENT, locale="en-IN")
    return context, context.new_page()


def _recon(browser, url):
    """One minimal page load in its own fresh, disposable context. Returns
    (block_reason_or_None, card_count)."""
    context, page = _fresh_context_and_page(browser)
    try:
        resp = page.goto(url, timeout=osc.PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        block_reason = osc._detect_nestasia_block(resp.status if resp else None, page.title())
        if block_reason:
            return block_reason, 0
        cards = page.eval_on_selector_all("[data-product-card]", osc._CARD_EXTRACT_JS)
        return None, len(cards)
    finally:
        context.close()


def main():
    now = datetime.now().isoformat()
    all_rows = []
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        print("=" * 70)
        print("STEP 1: single minimal recon -- jars-canisters, fresh isolated context")
        print("=" * 70)
        block_reason, card_count = _recon(browser, COLLECTIONS[0][1])
        if block_reason:
            print(f"RECON BLOCKED ({block_reason}). Stopping immediately per instructions -- "
                  f"not attempting jars-canisters or any of the other three collections.")
            browser.close()
            print()
            print("RESULT: recon blocked. No collections attempted. See report for recommendation.")
            return
        print(f"RECON CLEAN: HTTP 200, {card_count} real card(s) found, no block signature detected.")
        print("(Known from attempts 1-2: a clean single-page recon is necessary but not "
              "sufficient -- proceeding one isolated collection at a time, not assuming this "
              "means the rest will also be clean.)")
        print()

        prior_blocked = None  # None = no prior collection yet; True/False after the first
        for i, (subcategory, url) in enumerate(COLLECTIONS):
            print("=" * 70)
            print(f"STEP {i + 2}: {subcategory} ({url}) -- fresh isolated context")
            print("=" * 70)
            context, page = _fresh_context_and_page(browser)
            try:
                found, collection_complete = osc.collect_subcategory(page, url, subcategory)
            finally:
                context.close()

            blocked_this = not collection_complete
            if prior_blocked is True:
                spread_note = (" *** BLOCK SPREAD to this fresh, never-touched-before context too "
                                "-- isolation did NOT contain it ***") if blocked_this else \
                              (" (previous collection was blocked, but THIS fresh context worked "
                               "cleanly -- isolation DID contain it here)")
            else:
                spread_note = ""
            print(f"  -> {len(found)} SKU(s), collection_complete={collection_complete}{spread_note}")

            results.append({
                "subcategory": subcategory, "url": url, "sku_count": len(found),
                "collection_complete": collection_complete, "blocked": blocked_this,
            })
            prior_blocked = blocked_this

            for product_url, sku in found.items():
                sku["source_type"] = osc.SOURCE_TYPE
                sku["collected_at"] = now
                sku["collection_complete"] = collection_complete
                all_rows.append(sku)

            print()
            if i < len(COLLECTIONS) - 1:
                time.sleep(osc.DELAY_BETWEEN_PAGES_SECONDS + 1)

        browser.close()

    if all_rows:
        osc.append_catalogue(all_rows)
        print(f"Appended {len(all_rows)} row(s) to {osc.OUTPUT_FILE}.")
    else:
        print(f"No rows collected -- nothing appended to {osc.OUTPUT_FILE}.")

    print()
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    any_spread = False
    for idx, r in enumerate(results):
        flag = "" if r["collection_complete"] else "  *** PARTIAL -- blocked mid-pagination ***"
        print(f"  {r['subcategory']} ({r['url']}): {r['sku_count']} SKU(s), "
              f"collection_complete={r['collection_complete']}{flag}")
        if idx > 0 and results[idx - 1]["blocked"] and r["blocked"]:
            any_spread = True

    print()
    blocked_count = sum(1 for r in results if r["blocked"])
    print(f"Collections blocked: {blocked_count} of {len(results)}")
    if blocked_count == 0:
        print("HYPOTHESIS RESULT: no blocks occurred at all this run -- isolation question moot "
              "this time (nothing to contain).")
    elif any_spread:
        print("HYPOTHESIS RESULT: block SPREAD across fresh, isolated contexts -- isolation did "
              "NOT prevent it. This is evidence against the session-state theory (or evidence "
              "the block operates above the context level, e.g. IP-based).")
    else:
        print("HYPOTHESIS RESULT: at least one collection was blocked, but it did NOT spread to "
              "the next fresh-context collection -- isolation DID contain it. Supports the "
              "session-state theory.")


if __name__ == "__main__":
    main()
