"""
Google Trends + Google autocomplete collector -- search demand signals for
Nestasia and its tracked competitors, as opposed to own_site_collector.py/
competitor_collector.py (storefront SKU/price/stock scraping).

Built following this project's established conventions (see those two
scripts), applied at a level appropriate to the actual risk here:
  - config-driven input (search_terms.csv), no terms hardcoded in the script
  - every row carries source_type="search_signal" and a collected_at
    timestamp, matching the rest of this project's provenance discipline
  - failures are loud, not swallowed: a genuine block/rate-limit is logged
    and the affected term is skipped, never silently recorded as "no data"

Where this deliberately does NOT copy the storefront collectors: Google
Trends (pytrends) and Google's public autocomplete endpoint are free,
public APIs with no login wall and no anti-scraping arms race the way
Flipkart/Amazon/nestasia.in have -- there is no Cloudflare interstitial to
detect, no "declared tag vs. rendered button" ambiguity, and no reason for
9-second inter-page pacing or a 12-25-page cap built around a hostile
site's pagination. BUT this was checked empirically, not assumed just
because it's "the easier API": a live test run of pytrends' related_queries()
during development of this script hit a genuine HTTP 429 after a handful of
back-to-back calls with no delay at all -- Google Trends' undocumented rate
limit is real, tighter than a few-seconds-apart pace alone survives once a
few calls land close together, and NOT the same thing as "no blocking risk."
So this script still has a retry-with-backoff loop around every pytrends
call (RETRY_BACKOFF_SECONDS), it just doesn't need own_site_collector.py's
Cloudflare-interstitial-title detection, since a 429 response is Google's
own explicit signal -- there is no ambiguous "0 results" case to disambiguate
here the way there was for nestasia.in's blocked pagination.

Three genuinely different kinds of row come out of this (interest-over-time
points, related/rising queries, and autocomplete suggestions) -- kept as ONE
output file/table with a `signal_type` discriminator column, per the brief,
rather than three separate tables the way paid_ad_creative got its own table
(migration 001's rationale for a separate table was that an ad and a SKU
are genuinely unrelated entities; here all three rows describe the same
entity -- demand signal for one search term -- just captured three
different ways, so one table with intentionally-nullable, signal_type-
specific columns is the more honest fit, not a "pile of always-NULL
columns for an unrelated entity" the way migration 001 warned against).

Install (one-time):
    pip install pytrends

Usage:
    python search_signal_collector.py
Reads search_terms.csv (term, category, type: brand|generic). Appends a
fresh snapshot to search_signal_data.csv on every run -- same non-
incremental, point-in-time-facts rationale as own_site_collector.py and
competitor_collector.py (a Trends interest score and an autocomplete
suggestion list are both point-in-time facts that legitimately change
day to day).
"""

import csv
import logging
import os
import random
import re
import time
from datetime import datetime, timezone

import requests
from pytrends.request import TrendReq

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TERMS_FILE = "search_terms.csv"
OUTPUT_FILE = "search_signal_data.csv"
SOURCE_TYPE = "search_signal"

TRENDS_GEO = "IN"
TRENDS_TIMEFRAME = "today 3-m"
TRENDS_BATCH_SIZE = 5  # pytrends/Google Trends hard cap: max 5 terms per payload

# "Standard, brief pacing" per instructions -- these sites aren't fighting
# bots the way the storefronts are. But see the module docstring: a real
# 429 was hit during development, so a retry-with-backoff loop backs this
# up rather than assuming a few seconds alone is always enough.
DELAY_BETWEEN_CALLS_SECONDS = 4
RETRY_BACKOFF_SECONDS = [15, 45, 90]  # escalating waits on a 429/rate-limit

AUTOCOMPLETE_URL = "https://suggestqueries.google.com/complete/search"
AUTOCOMPLETE_TIMEOUT_S = 10

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

# Strips the "cookware"/"kitchen" qualifier back off a brand term (e.g.
# "Home Centre kitchen" -> "Home Centre") to get the bare brand name for
# the autocomplete lookup -- search_terms.csv only has term/category/type
# columns per the brief, so the bare brand is derived rather than stored
# as a fourth column.
_BRAND_SUFFIX_RE = re.compile(r"^(.*?)\s+(cookware|kitchen)$", re.IGNORECASE)


def _bare_brand_name(term):
    m = _BRAND_SUFFIX_RE.match(term.strip())
    return m.group(1) if m else term.strip()


# ---------------------------------------------------------------------------
# Retry wrapper -- every pytrends call goes through this, not called raw.
# ---------------------------------------------------------------------------
def _with_retry(fn, description):
    """Runs fn() with escalating backoff on a rate-limit/5xx-shaped failure.
    Raises the last exception if every retry is exhausted -- a genuinely
    unrecoverable failure must be loud, never silently treated as "no data
    for this term." """
    last_exc = None
    for attempt, wait_s in enumerate([0] + RETRY_BACKOFF_SECONDS, start=1):
        if wait_s:
            log.warning(f"  {description}: retry {attempt - 1}/{len(RETRY_BACKOFF_SECONDS)} "
                        f"after {wait_s}s backoff...")
            time.sleep(wait_s)
        try:
            return fn()
        except Exception as e:
            last_exc = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            looks_rate_limited = status == 429 or "429" in str(e) or "TooManyRequests" in type(e).__name__
            if not looks_rate_limited:
                # Not a rate-limit shape -- don't burn through the retry
                # budget on an error backoff can't fix (e.g. a malformed
                # term or a genuine network failure worth seeing immediately).
                raise
            log.warning(f"  {description}: rate-limited ({e})")
    raise last_exc


# ---------------------------------------------------------------------------
# Google Trends: interest-over-time + related/rising queries, batched
# ---------------------------------------------------------------------------
def _batches(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _collect_trends_batch(pytrends, batch, batch_id, now_iso):
    """Returns (interest_rows, related_rows) for one <=5-term batch. Interest
    values are Google Trends' 0-100 relative-within-this-batch index -- only
    comparable to each other WITHIN the same batch_id, never across batches
    (Trends does not expose a cross-batch-comparable absolute number). This
    is inherent to how Trends works, not a shortcoming of this collector --
    batch_id is carried on every row specifically so a later reader can't
    accidentally compare interest_value across two different batches."""
    terms = [t["term"] for t in batch]
    term_meta = {t["term"]: t for t in batch}

    def _build_and_fetch_interest():
        pytrends.build_payload(terms, timeframe=TRENDS_TIMEFRAME, geo=TRENDS_GEO)
        return pytrends.interest_over_time()

    df = _with_retry(_build_and_fetch_interest, f"batch {batch_id} interest_over_time({terms})")

    interest_rows = []
    if df is None or df.empty:
        log.warning(f"  batch {batch_id}: interest_over_time returned no data for {terms}")
    else:
        for date, row in df.iterrows():
            for term in terms:
                if term not in row:
                    continue
                meta = term_meta[term]
                interest_rows.append({
                    "signal_type": "interest_over_time",
                    "term": term,
                    "category": meta["category"],
                    "term_type": meta["type"],
                    "batch_id": batch_id,
                    "date": date.strftime("%Y-%m-%d"),
                    "interest_value": int(row[term]),
                    "is_partial": bool(row.get("isPartial", False)),
                    "query_type": "",
                    "related_query": "",
                    "related_value_raw": "",
                    "related_value_numeric": "",
                    "suggestion_rank": "",
                    "suggestion_text": "",
                    "source_type": SOURCE_TYPE,
                    "collected_at": now_iso,
                })
    log.info(f"  batch {batch_id} interest_over_time: {len(interest_rows)} rows across {len(terms)} term(s)")

    time.sleep(DELAY_BETWEEN_CALLS_SECONDS + random.uniform(0, 1.5))

    def _fetch_related():
        return pytrends.related_queries()

    related = _with_retry(_fetch_related, f"batch {batch_id} related_queries({terms})")

    related_rows = []
    for term in terms:
        meta = term_meta[term]
        term_result = (related or {}).get(term) or {}
        for query_type in ("top", "rising"):
            rq_df = term_result.get(query_type)
            if rq_df is None or rq_df.empty:
                continue
            for _, r in rq_df.iterrows():
                raw_value = r.get("value")
                numeric_value = ""
                try:
                    numeric_value = int(raw_value)
                except (TypeError, ValueError):
                    pass  # e.g. rising queries can report "Breakout" instead of a number
                related_rows.append({
                    "signal_type": "related_query",
                    "term": term,
                    "category": meta["category"],
                    "term_type": meta["type"],
                    "batch_id": batch_id,
                    "date": "",
                    "interest_value": "",
                    "is_partial": "",
                    "query_type": query_type,
                    "related_query": r.get("query"),
                    "related_value_raw": str(raw_value),
                    "related_value_numeric": numeric_value,
                    "suggestion_rank": "",
                    "suggestion_text": "",
                    "source_type": SOURCE_TYPE,
                    "collected_at": now_iso,
                })
    log.info(f"  batch {batch_id} related_queries: {len(related_rows)} rows across {len(terms)} term(s)")

    return interest_rows, related_rows


# ---------------------------------------------------------------------------
# Google autocomplete
# ---------------------------------------------------------------------------
def _collect_autocomplete(brand_term, now_iso):
    """One direct HTTP call to Google's public suggestqueries endpoint --
    no API key, no library. Returns rows for this brand's suggestion list,
    in the order Google returned them (rank 1 = first/most-prominent
    suggestion)."""
    brand_name = _bare_brand_name(brand_term)
    params = {"client": "chrome", "q": brand_name, "hl": "en", "gl": "in"}

    def _fetch():
        resp = requests.get(AUTOCOMPLETE_URL, params=params, timeout=AUTOCOMPLETE_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()

    try:
        data = _with_retry(_fetch, f"autocomplete({brand_name!r})")
    except Exception as e:
        log.warning(f"  autocomplete FAILED for {brand_name!r}: {e} -- skipping this brand, not recording empty data.")
        return []

    suggestions = data[1] if isinstance(data, list) and len(data) > 1 else []
    rows = []
    for rank, suggestion in enumerate(suggestions, start=1):
        rows.append({
            "signal_type": "autocomplete",
            "term": brand_name,
            "category": "",
            "term_type": "brand",
            "batch_id": "",
            "date": "",
            "interest_value": "",
            "is_partial": "",
            "query_type": "",
            "related_query": "",
            "related_value_raw": "",
            "related_value_numeric": "",
            "suggestion_rank": rank,
            "suggestion_text": suggestion,
            "source_type": SOURCE_TYPE,
            "collected_at": now_iso,
        })
    log.info(f"  autocomplete({brand_name!r}): {len(rows)} suggestion(s)")
    return rows


# ---------------------------------------------------------------------------
# Output dataset handling
# ---------------------------------------------------------------------------
FIELDNAMES = [
    "signal_type", "term", "category", "term_type", "batch_id",
    "date", "interest_value", "is_partial",
    "query_type", "related_query", "related_value_raw", "related_value_numeric",
    "suggestion_rank", "suggestion_text",
    "source_type", "collected_at",
]


def append_output(rows):
    file_exists = os.path.exists(OUTPUT_FILE)
    with open(OUTPUT_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def main():
    if not os.path.exists(TERMS_FILE):
        log.error(f"{TERMS_FILE} not found.")
        return

    with open(TERMS_FILE, newline="", encoding="utf-8") as f:
        terms = [
            {"term": row["term"].strip(), "category": row["category"].strip(), "type": row["type"].strip()}
            for row in csv.DictReader(f)
        ]
    log.info(f"Loaded {len(terms)} term(s) from {TERMS_FILE}")

    now_iso = datetime.now(timezone.utc).isoformat()
    pytrends = TrendReq(hl="en-IN", tz=330)  # tz=330 = IST offset in minutes, matches geo=IN

    all_rows = []
    interest_count = 0
    related_count = 0
    autocomplete_count = 0
    failed_batches = []
    failed_brands = []

    for batch_id, batch in enumerate(_batches(terms, TRENDS_BATCH_SIZE), start=1):
        batch_terms = [t["term"] for t in batch]
        log.info(f"Trends batch {batch_id}: {batch_terms}")
        try:
            interest_rows, related_rows = _collect_trends_batch(pytrends, batch, batch_id, now_iso)
        except Exception as e:
            log.error(f"  batch {batch_id} FAILED after all retries: {e} -- skipping this batch entirely, "
                      f"not recording partial/fabricated data for it.")
            failed_batches.append(batch_terms)
            continue
        all_rows.extend(interest_rows)
        all_rows.extend(related_rows)
        interest_count += len(interest_rows)
        related_count += len(related_rows)
        time.sleep(DELAY_BETWEEN_CALLS_SECONDS + random.uniform(0, 1.5))

    brand_terms = [t["term"] for t in terms if t["type"] == "brand"]
    for brand_term in brand_terms:
        rows = _collect_autocomplete(brand_term, now_iso)
        if not rows:
            failed_brands.append(brand_term)
        all_rows.extend(rows)
        autocomplete_count += len(rows)
        time.sleep(DELAY_BETWEEN_CALLS_SECONDS + random.uniform(0, 1.5))

    if all_rows:
        append_output(all_rows)

    print()
    print("=" * 60)
    print("Search signal collection summary")
    print("=" * 60)
    print(f"  Terms tracked: {len(terms)} ({len(brand_terms)} brand, {len(terms) - len(brand_terms)} generic)")
    print(f"  interest_over_time rows: {interest_count}")
    print(f"  related_query rows:      {related_count}")
    print(f"  autocomplete rows:       {autocomplete_count}")
    print(f"  Total rows this run:     {len(all_rows)}")
    if failed_batches:
        print(f"  WARNING: {len(failed_batches)} Trends batch(es) failed after retries and were skipped: {failed_batches}")
    if failed_brands:
        print(f"  WARNING: {len(failed_brands)} brand(s) had no autocomplete data (failed or empty): {failed_brands}")
    if not failed_batches and not failed_brands:
        print("  All batches and brands returned data successfully.")
    print("=" * 60)


if __name__ == "__main__":
    main()
