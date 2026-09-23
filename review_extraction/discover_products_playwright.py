"""
Playwright-based product discovery -- uses a real (headless) Chromium
browser instead of the `requests` library, since Flipkart blocks plain
HTTP requests (confirmed: even from residential IPs, even with realistic
headers) but allows actual browser traffic.

Install (one-time):
    pip install playwright
    playwright install chromium

Usage:
    python3 discover_products_playwright.py
Reads brand_categories.csv, writes/appends to products.csv -- same file
format as the original discover_products.py, so extract_reviews.py works
unchanged.
"""

import csv
import os
import time
import random
import logging
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright

BRAND_CATEGORIES_FILE = "brand_categories.csv"
PRODUCTS_FILE = "products.csv"
PAGE_LOAD_TIMEOUT_MS = 20000
DELAY_BETWEEN_PAGES_SECONDS = 9      # more conservative pacing -- lowers detection risk with a real browser
MAX_PAGES_PER_CATEGORY = 6           # relevance drops fast past a few pages of results; also caps detection risk

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


def discover_flipkart_products(page, category_url, brand):
    found = {}
    base_url = category_url.split("?")[0]

    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        separator = "&" if "?" in category_url else "?"
        page_url = f"{category_url}{separator}page={page_num}" if page_num > 1 else category_url

        try:
            page.goto(page_url, timeout=PAGE_LOAD_TIMEOUT_MS, wait_until="domcontentloaded")
            # give any lazy-loaded product grid a moment to render
            page.wait_for_timeout(2000)
        except Exception as e:
            log.warning(f"  page {page_num} load failed: {e}")
            break

        # Grab every link on the page that points to a product detail page
        links = page.eval_on_selector_all(
            "a[href*='/p/itm']",
            "els => els.map(e => ({href: e.getAttribute('href'), text: e.getAttribute('title') || e.innerText}))"
        )

        new_this_page = 0
        for link in links:
            href = link.get("href")
            if not href or "/p/itm" not in href:
                continue
            full_url = urljoin("https://www.flipkart.com", href.split("?")[0])
            name = (link.get("text") or "").strip()
            if not name:
                continue
            if full_url not in found:
                found[full_url] = name
                new_this_page += 1

        log.info(f"  page {page_num}: {new_this_page} new product links (running total {len(found)})")

        if new_this_page == 0:
            log.info(f"  no new products on page {page_num}, stopping pagination for {brand}")
            break

        time.sleep(DELAY_BETWEEN_PAGES_SECONDS + random.uniform(0, 1.5))

    return found


def load_existing_urls():
    existing = set()
    if os.path.exists(PRODUCTS_FILE):
        with open(PRODUCTS_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add(row.get("url", "").strip())
    return existing


def append_products(rows):
    file_exists = os.path.exists(PRODUCTS_FILE)
    with open(PRODUCTS_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["brand", "product_name", "platform", "url"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    if not os.path.exists(BRAND_CATEGORIES_FILE):
        log.error(f"{BRAND_CATEGORIES_FILE} not found.")
        return

    existing_urls = load_existing_urls()
    all_new_rows = []

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

        with open(BRAND_CATEGORIES_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                brand = row["brand"].strip()
                platform = row["platform"].strip().lower()
                category_url = row["category_url"].strip()

                if platform != "flipkart":
                    log.warning(f"Only Flipkart is supported in this Playwright version so far, skipping {brand}")
                    continue

                log.info(f"Discovering products for {brand} on {platform} (via browser)...")
                found = discover_flipkart_products(page, category_url, brand)
                log.info(f"  -> {len(found)} total product links found for {brand}")

                for url, name in found.items():
                    if url in existing_urls:
                        continue
                    all_new_rows.append({
                        "brand": brand,
                        "product_name": name[:120],
                        "platform": platform,
                        "url": url,
                    })
                    existing_urls.add(url)

        browser.close()

    if all_new_rows:
        append_products(all_new_rows)
        log.info(f"Added {len(all_new_rows)} new products to {PRODUCTS_FILE}")
    else:
        log.info("No new products discovered.")


if __name__ == "__main__":
    main()
