"""
Incremental review extraction for Nestasia's Kitchen category competitor set.

Design principle: NOT real-time. Pulls only reviews newer than the last
checkpoint per product, so re-running daily/weekly is cheap and idempotent.
Schedule it yourself (cron / Task Scheduler) at whatever cadence you choose
-- see README.md. Nothing in this script polls continuously.
"""

import csv
import json
import os
import time
import random
import logging
from datetime import datetime
from dateutil import parser as dateparser

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PRODUCTS_FILE = "products.csv"
CHECKPOINT_FILE = "checkpoint.json"
OUTPUT_FILE = "reviews_dataset.csv"
LOG_FILE = "run.log"

REQUEST_DELAY_SECONDS = 3          # pause between requests -- be polite
REQUEST_TIMEOUT = 15
MAX_PAGES_PER_PRODUCT = 10          # safety cap so one product can't run away

# The real Amazon bot-check interstitial captured during the Sep 2026 diagnostic
# was ~3.8KB; real product pages are far larger. Secondary/supporting signal only
# (see _detect_amazon_block) -- don't trust page size alone, it's fragile.
AMAZON_INTERSTITIAL_SIZE_THRESHOLD = 5000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Checkpoint handling (this is what makes it "incremental", not real-time)
# ---------------------------------------------------------------------------
def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r") as f:
            return json.load(f)
    return {}


def save_checkpoint(checkpoint):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f, indent=2, default=str)


def product_key(brand, product_name, url):
    return f"{brand}|{product_name}|{url}"


# ---------------------------------------------------------------------------
# Output dataset handling
# ---------------------------------------------------------------------------
FIELDNAMES = [
    "brand", "product_name", "platform", "rating", "review_date",
    "review_text", "reviewer_name", "verified_purchase", "source_url",
    "collected_at",
]


def load_existing_review_keys():
    """(brand, product_name, review_date, review_text) tuples already on disk."""
    existing = set()
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.add((
                    row.get("brand", ""),
                    row.get("product_name", ""),
                    row.get("review_date", ""),
                    row.get("review_text", ""),
                ))
    return existing


def append_reviews(rows):
    # Dedup against the CSV itself, not just checkpoint.json -- if the checkpoint
    # file is ever lost, every review looks "new" again, and without this check
    # they'd all be re-appended as duplicates.
    existing_keys = load_existing_review_keys()
    new_rows = []
    for row in rows:
        key = (row.get("brand", ""), row.get("product_name", ""), row.get("review_date", ""), row.get("review_text", ""))
        if key in existing_keys:
            continue
        existing_keys.add(key)
        new_rows.append(row)

    if not new_rows:
        return 0

    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)

    return len(new_rows)


# ---------------------------------------------------------------------------
# Platform-specific parsers
# ---------------------------------------------------------------------------
def fetch_flipkart_reviews(url):
    """
    Flipkart product pages embed review blocks server-side, so a plain GET
    + BeautifulSoup parse usually works without needing a headless browser.
    Selectors below match Flipkart's review-card structure as of this
    writing; Flipkart changes markup periodically -- if this starts
    returning zero rows, inspect the page and update the selectors below.
    """
    reviews = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.warning(f"Flipkart fetch failed for {url}: {e}")
        return reviews

    soup = BeautifulSoup(resp.text, "html.parser")

    # Flipkart review cards -- update these class names if the layout changes
    review_cards = soup.select("div._27M-vq, div.col.EPCmJX")

    for card in review_cards:
        try:
            rating_el = card.select_one("div._3LWZlK")
            rating = rating_el.get_text(strip=True) if rating_el else None

            text_el = card.select_one("div.t-ZTKy")
            review_text = text_el.get_text(" ", strip=True) if text_el else None

            reviewer_el = card.select_one("p._2sc7ZR._2V5EHH")
            reviewer_name = reviewer_el.get_text(strip=True) if reviewer_el else None

            date_el = card.select_one("p._2sc7ZR")
            review_date = date_el.get_text(strip=True) if date_el else None

            if review_text:
                reviews.append({
                    "rating": rating,
                    "review_date": review_date,
                    "review_text": review_text,
                    "reviewer_name": reviewer_name,
                    "verified_purchase": "Unknown",  # Flipkart doesn't always expose this cleanly
                })
        except Exception as e:
            log.debug(f"Skipped a malformed review card: {e}")
            continue

    return reviews


def _detect_amazon_block(resp, soup):
    """
    Returns a reason string if `resp` looks like Amazon's bot-check
    interstitial rather than a real product page, else None.

    Confirmed (Sep 2026 diagnostic, one product): status 200, body is a
    "Click the button below to continue shopping" page whose form posts to
    /errors_page/validateCaptcha -- not the real product page. See NOTES.md
    finding (c). Checks are deliberate signals, not a loose "captcha"
    substring match (which only worked by coincidence, since that word
    happens to appear in the interstitial's form-action URL).
    """
    if "validateCaptcha" in resp.text:
        return "response contains a validateCaptcha interstitial form"

    if soup.select_one("#productTitle") is None:
        reason = "missing #productTitle (expected product-page marker)"
        if len(resp.text) < AMAZON_INTERSTITIAL_SIZE_THRESHOLD:
            reason += f", and response unusually small ({len(resp.text)} bytes)"
        return reason

    return None


def fetch_amazon_reviews(url):
    """
    Amazon.in has significantly stronger anti-bot measures than Flipkart.
    A plain requests.get() will often be blocked or served a bot-check
    interstitial (see _detect_amazon_block). This function attempts a basic
    fetch and logs a clear failure rather than retrying aggressively (retry
    storms are what get IPs banned).

    If this consistently fails for you, the proposal's own risk table
    already anticipates this ("Marketplace anti-scraping measures slow
    review collection -> Manual fallback collection") -- see NOTES.md for
    a manual collection template that covers this gap without violating
    Amazon's terms.
    """
    reviews = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        log.warning(f"Amazon fetch failed for {url}: {e}")
        return reviews

    if resp.status_code != 200:
        log.warning(f"Amazon block/interstitial detected (reason: non-200 status {resp.status_code}): {url}")
        return reviews

    soup = BeautifulSoup(resp.text, "html.parser")

    block_reason = _detect_amazon_block(resp, soup)
    if block_reason:
        log.warning(f"Amazon block/interstitial detected (reason: {block_reason}): {url}")
        return reviews

    review_cards = soup.select("div[data-hook='review']")

    for card in review_cards:
        try:
            rating_el = card.select_one("i[data-hook='review-star-rating'] span")
            rating = rating_el.get_text(strip=True) if rating_el else None

            title_el = card.select_one("a[data-hook='review-title']")
            title = title_el.get_text(strip=True) if title_el else ""

            body_el = card.select_one("span[data-hook='review-body']")
            body = body_el.get_text(" ", strip=True) if body_el else ""
            review_text = f"{title} — {body}".strip(" —")

            date_el = card.select_one("span[data-hook='review-date']")
            review_date = date_el.get_text(strip=True) if date_el else None

            reviewer_el = card.select_one(".a-profile-name")
            reviewer_name = reviewer_el.get_text(strip=True) if reviewer_el else None

            verified_el = card.select_one("span[data-hook='avp-badge']")
            verified = "Yes" if verified_el else "Unknown"

            if review_text.strip(" —"):
                reviews.append({
                    "rating": rating,
                    "review_date": review_date,
                    "review_text": review_text,
                    "reviewer_name": reviewer_name,
                    "verified_purchase": verified,
                })
        except Exception as e:
            log.debug(f"Skipped a malformed Amazon review card: {e}")
            continue

    return reviews


PLATFORM_FETCHERS = {
    "flipkart": fetch_flipkart_reviews,
    "amazon": fetch_amazon_reviews,
}


# ---------------------------------------------------------------------------
# Incremental filtering: this is the "not real-time, but always fresh" logic
# ---------------------------------------------------------------------------
def parse_review_date(raw):
    if not raw:
        return None
    try:
        return dateparser.parse(raw, fuzzy=True)
    except Exception:
        return None


def filter_new_reviews(reviews, last_seen_date):
    """Keep only reviews dated after the last checkpoint for this product."""
    if last_seen_date is None:
        return reviews  # first run for this product -- take everything

    last_seen = dateparser.parse(last_seen_date) if isinstance(last_seen_date, str) else last_seen_date
    new_reviews = []
    for r in reviews:
        parsed_date = parse_review_date(r.get("review_date"))
        if parsed_date is None or parsed_date > last_seen:
            new_reviews.append(r)
    return new_reviews


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------
def main():
    if not os.path.exists(PRODUCTS_FILE):
        log.error(f"{PRODUCTS_FILE} not found. Create it first -- see README.md")
        return

    checkpoint = load_checkpoint()
    all_new_rows = []
    products_processed = 0
    products_skipped = 0

    with open(PRODUCTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            brand = row["brand"].strip()
            product_name = row["product_name"].strip()
            platform = row["platform"].strip().lower()
            url = row["url"].strip()

            fetcher = PLATFORM_FETCHERS.get(platform)
            if not fetcher:
                log.warning(f"Unknown platform '{platform}' for {product_name}, skipping")
                products_skipped += 1
                continue

            key = product_key(brand, product_name, url)
            last_seen = checkpoint.get(key, {}).get("last_review_date")

            log.info(f"Fetching {brand} — {product_name} ({platform})...")
            reviews = fetcher(url)

            new_reviews = filter_new_reviews(reviews, last_seen)

            if not reviews:
                log.info(f"  -> 0 reviews found (page structure may differ or fetch was blocked)")
            elif not new_reviews:
                log.info(f"  -> {len(reviews)} reviews found, 0 new since last checkpoint")
            else:
                log.info(f"  -> {len(reviews)} reviews found, {len(new_reviews)} new")

            now = datetime.now().isoformat()
            for r in new_reviews:
                all_new_rows.append({
                    "brand": brand,
                    "product_name": product_name,
                    "platform": platform,
                    "rating": r.get("rating"),
                    "review_date": r.get("review_date"),
                    "review_text": r.get("review_text"),
                    "reviewer_name": r.get("reviewer_name"),
                    "verified_purchase": r.get("verified_purchase"),
                    "source_url": url,
                    "collected_at": now,
                })

            # Update checkpoint with the newest review date seen this run
            all_dates = [parse_review_date(r.get("review_date")) for r in reviews]
            all_dates = [d for d in all_dates if d is not None]
            if all_dates:
                newest = max(all_dates)
                checkpoint[key] = {"last_review_date": newest.isoformat(), "last_run": now}

            products_processed += 1
            time.sleep(REQUEST_DELAY_SECONDS + random.uniform(0, 1.5))

    written = append_reviews(all_new_rows) if all_new_rows else 0

    save_checkpoint(checkpoint)

    log.info(
        f"Run complete. Products processed: {products_processed}, "
        f"skipped: {products_skipped}, new reviews written: {written} "
        f"({len(all_new_rows) - written} skipped as already-present duplicates)"
    )
    log.info(f"Dataset: {OUTPUT_FILE} | Checkpoint: {CHECKPOINT_FILE}")


if __name__ == "__main__":
    main()
