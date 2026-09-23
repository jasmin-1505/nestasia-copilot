"""
Auto-discovery: crawls a brand's category/listing page on Flipkart or Amazon
and extracts every product URL + name on it (paginating through all result
pages), then writes/appends them to products.csv in the format
extract_reviews.py expects.

This removes the need to manually find and paste individual product URLs.
You provide ONE listing-page URL per brand (e.g. "all Nestasia cookware on
Flipkart"), and this script does the rest.

Usage:
    python3 discover_products.py
Reads brand_categories.csv, writes/appends to products.csv.
"""

import csv
import os
import time
import random
import logging
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup

BRAND_CATEGORIES_FILE = "brand_categories.csv"
PRODUCTS_FILE = "products.csv"
REQUEST_DELAY_SECONDS = 3
REQUEST_TIMEOUT = 15
MAX_PAGES_PER_CATEGORY = 15   # safety cap -- adjust if a brand has more pages of products

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)


def add_page_param(url, page_num, platform):
    """Add/replace the pagination query param for the given platform."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    if platform == "flipkart":
        qs["page"] = [str(page_num)]
    parsed = parsed._replace(query=urlencode(qs, doseq=True))
    return urlunparse(parsed)


def discover_flipkart_products(category_url, brand):
    """
    Flipkart category/brand listing pages show product cards with links
    like /product-name/p/itmXXXXXXXXXXXXX. We paginate via ?page=N and stop
    when a page returns no new product links.
    """
    found = {}  # url -> name, dedup by url
    for page_num in range(1, MAX_PAGES_PER_CATEGORY + 1):
        page_url = add_page_param(category_url, page_num, "flipkart")
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning(f"  page {page_num} fetch failed: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")

        # Product links on Flipkart listing pages: <a> tags whose href contains "/p/itm"
        links = soup.select("a[href*='/p/itm']")
        new_this_page = 0
        for a in links:
            href = a.get("href", "")
            if "/p/itm" not in href:
                continue
            full_url = urljoin("https://www.flipkart.com", href.split("?")[0])
            # Try to get a readable product name from the link text or nearby title attr
            name = a.get("title") or a.get_text(strip=True)
            if not name:
                continue
            if full_url not in found:
                found[full_url] = name
                new_this_page += 1

        log.info(f"  page {page_num}: {new_this_page} new product links (running total {len(found)})")

        if new_this_page == 0:
            log.info(f"  no new products on page {page_num}, stopping pagination for {brand}")
            break

        time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 1.5))

    return found


def discover_amazon_products(category_url, brand):
    """
    Amazon search/category result pages are heavily bot-protected -- a plain
    requests.get() is likely to hit a CAPTCHA wall. This function attempts
    it and logs a clear signal if blocked, rather than pretending it
    succeeded. See NOTES.md for the manual fallback approach for Amazon.
    """
    found = {}
    try:
        resp = requests.get(category_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200 or "captcha" in resp.text.lower():
            log.warning(f"  Amazon likely blocked discovery for {brand} -- use manual fallback (see NOTES.md)")
            return found
    except requests.RequestException as e:
        log.warning(f"  Amazon fetch failed for {brand}: {e}")
        return found

    soup = BeautifulSoup(resp.text, "html.parser")
    links = soup.select("a.a-link-normal.s-no-outline, h2 a.a-link-normal")
    for a in links:
        href = a.get("href", "")
        if "/dp/" not in href:
            continue
        full_url = urljoin("https://www.amazon.in", href.split("?")[0])
        name = a.get_text(strip=True)
        if name and full_url not in found:
            found[full_url] = name

    return found


DISCOVERERS = {
    "flipkart": discover_flipkart_products,
    "amazon": discover_amazon_products,
}


def load_existing_urls():
    """Avoid writing duplicate rows if products.csv already has entries."""
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
        log.error(f"{BRAND_CATEGORIES_FILE} not found. Create it first -- one row per "
                  f"brand+platform with a category/listing page URL.")
        return

    existing_urls = load_existing_urls()
    all_new_rows = []

    with open(BRAND_CATEGORIES_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            brand = row["brand"].strip()
            platform = row["platform"].strip().lower()
            category_url = row["category_url"].strip()

            discoverer = DISCOVERERS.get(platform)
            if not discoverer:
                log.warning(f"Unknown platform '{platform}' for {brand}, skipping")
                continue

            log.info(f"Discovering products for {brand} on {platform}...")
            found = discoverer(category_url, brand)
            log.info(f"  -> {len(found)} total product links found for {brand}")

            for url, name in found.items():
                if url in existing_urls:
                    continue
                all_new_rows.append({
                    "brand": brand,
                    "product_name": name[:120],  # keep names reasonably short
                    "platform": platform,
                    "url": url,
                })
                existing_urls.add(url)

    if all_new_rows:
        append_products(all_new_rows)
        log.info(f"Added {len(all_new_rows)} new products to {PRODUCTS_FILE}")
    else:
        log.info("No new products discovered (or all already present in products.csv)")


if __name__ == "__main__":
    main()
