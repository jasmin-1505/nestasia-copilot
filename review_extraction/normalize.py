"""
Builds a single cleaned dataset from every collection script's raw output
(nestasia_catalogue.csv + competitor_catalogue.csv), for downstream analysis.

This is NOT the generic version from the original plan -- every rule below
traces to something this project's own collection sessions actually found,
not a hypothetical:

1. Duplicate-case tag merging -- material_type_tag (own_site_collector.py's
   only tag-like field) is folded case-insensitively so e.g. "Glass" and
   "glass" count as one tag. Runs even though today's data happens to have
   no case collisions -- the point is not to rediscover this the day a
   collector starts emitting mixed casing.
2. collection_complete is a category-level fact, not a per-SKU one, and
   must never be silently absorbed into a clean-looking total. Several
   nestasia_catalogue.csv rows (Container, Lunch Boxes+Bags) are honestly
   marked collection_complete=False -- see NOTES.md finding (g). This script
   keeps that flag on every row AND produces a separate per-(brand,category)
   summary (normalize_summary.csv) so "12 SKUs, confirmed complete" and
   "12 SKUs, known partial" stay distinguishable even after rows are merged.
3. Home Centre's Bakeware category is a confirmed subset of its Cookware
   category (NOTES.md finding (i)) -- not two independent product sets.
   Cross-collection dedup (item 6) is written generically (group by
   product_url, not brand-specific logic) so this falls out of the same
   mechanism that handles re-run duplicates, rather than a special case.
4. stock_mismatch is a three/four-way signal across collectors --
   True/False (a real signal existed, e.g. nestasia.in, Milton, Wonderchef,
   Prestige), "N/A" (no comparable signal exists at all, e.g. Home Centre --
   see NOTES.md finding (i)), or "Unknown" (a genuinely ambiguous case that
   was deliberately not forced into True/False, e.g. Milton/Prestige
   configurable products -- see findings (l) and (m)). Collapsing "N/A" or
   "Unknown" into False would misrepresent an untested or inapplicable case
   as a confirmed-clean one -- exactly the mistake this project has spent
   several sessions avoiding. This script buckets into exactly these four
   values and keeps the original detail string in a separate column.
5. Price fields are parsed to a single numeric type; a row whose price is
   missing or unparseable is FLAGGED (price_flag, price_raw columns), never
   silently dropped or guessed. Confirmed against real data: 10 Prestige
   rows have a genuinely empty price -- all 10 are Out of Stock items whose
   grid card simply doesn't render a price box (a real gap in that
   collector's extraction, not a normalize.py bug to paper over).
6. Cross-collection SKU dedup -- the same (brand, product_url) can appear
   more than once because the raw catalogues are intentionally append-only
   (own_site_collector.py's and competitor_collector.py's own docstrings say
   so explicitly: every run's rows are kept, not deduped against past runs).
   Confirmed in real data: Wonderchef Cookware+Bakeware rows appear exactly
   twice (203 duplicate (brand,url) pairs, from two separate
   competitor_collector.py runs), and nestasia.in's jars-canisters partial
   result appears twice (10 duplicate URLs, from two separate blocked
   attempts a day apart). The most recent row (by collected_at) wins as the
   canonical data; older duplicates are dropped, not silently ignored --
   this script reports exactly how many.

Usage:
    python normalize.py
Reads nestasia_catalogue.csv and competitor_catalogue.csv from the current
directory. Writes normalized_catalogue.csv (one row per unique SKU) and
normalize_summary.csv (one row per brand+category, with SKU counts and
collection_complete status). Prints a report of every rule's effect on the
real data -- this is not meant to run silently.
"""

import csv
import logging
import os
import re
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

NESTASIA_FILE = "nestasia_catalogue.csv"
COMPETITOR_FILE = "competitor_catalogue.csv"
OUTPUT_FILE = "normalized_catalogue.csv"
SUMMARY_FILE = "normalize_summary.csv"

OUTPUT_FIELDNAMES = [
    "brand", "category", "categories", "product_name", "product_url",
    "price", "price_flag", "price_raw", "compare_at_price", "discount_percent",
    "material_type_tag", "stock_status_tag", "actual_button_state",
    "stock_mismatch", "stock_mismatch_detail", "collection_complete",
    "source_type", "collected_at",
]


# ---------------------------------------------------------------------------
# 1. Loading + schema unification
# ---------------------------------------------------------------------------
def _load_nestasia_rows():
    if not os.path.exists(NESTASIA_FILE):
        log.warning(f"{NESTASIA_FILE} not found -- skipping.")
        return []
    with open(NESTASIA_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["brand"] = "Nestasia"
        r["category"] = r.pop("subcategory")
        r["compare_at_price"] = ""  # own_site_collector.py doesn't capture this field at all
    log.info(f"Loaded {len(rows)} rows from {NESTASIA_FILE}")
    return rows


def _load_competitor_rows():
    if not os.path.exists(COMPETITOR_FILE):
        log.warning(f"{COMPETITOR_FILE} not found -- skipping.")
        return []
    with open(COMPETITOR_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["material_type_tag"] = ""  # competitor_collector.py doesn't capture this field
    log.info(f"Loaded {len(rows)} rows from {COMPETITOR_FILE}")
    return rows


# ---------------------------------------------------------------------------
# 2. Duplicate-case tag merging
# ---------------------------------------------------------------------------
def merge_duplicate_case_tags(rows, field):
    """Folds case-only variants of a tag field (e.g. "Glass" / "glass") into
    one canonical form -- the most frequently occurring original casing wins,
    so this doesn't invent a casing style that never appeared in the source
    data. Returns the number of distinct raw values that got folded into an
    existing canonical tag (0 if the data has no case collisions -- that's a
    real, reportable outcome, not a sign the function didn't run).
    """
    by_lower = defaultdict(lambda: defaultdict(int))
    for r in rows:
        val = (r.get(field) or "").strip()
        if not val:
            continue
        by_lower[val.lower()][val] += 1

    canonical = {}
    merges = 0
    for lower, variants in by_lower.items():
        winner = max(variants.items(), key=lambda kv: kv[1])[0]
        canonical[lower] = winner
        if len(variants) > 1:
            merges += len(variants) - 1  # N variants -> 1 canonical form = N-1 merges

    for r in rows:
        val = (r.get(field) or "").strip()
        if val:
            r[field] = canonical[val.lower()]

    return merges


# ---------------------------------------------------------------------------
# 3. Price / numeric parsing -- flag failures, never guess
# ---------------------------------------------------------------------------
_NUMERIC_STRIP_RE = re.compile(r"[₹$,\s]")


def _parse_numeric(raw):
    """Returns (value, flag) where flag is None on success, "empty" if the
    source field was blank, or "unparseable" if it had content that still
    didn't parse as a number. Never returns a guessed value."""
    if raw is None or raw.strip() == "":
        return None, "empty"
    cleaned = _NUMERIC_STRIP_RE.sub("", raw.strip())
    try:
        return float(cleaned), None
    except ValueError:
        return None, "unparseable"


# ---------------------------------------------------------------------------
# 4. stock_mismatch: bucket into exactly True / False / "Unknown" / "N/A",
#    never collapsed further, with the original detail preserved separately.
# ---------------------------------------------------------------------------
def _bucket_stock_mismatch(raw):
    s = (raw or "").strip()
    if s == "True":
        return "True", s
    if s == "False":
        return "False", s
    if s.startswith("N/A"):
        return "N/A", s
    if s.startswith("Unknown"):
        return "Unknown", s
    # Defensive fallback for any value not matching a known collector's
    # output shape: "Unknown" is the only safe default here -- silently
    # falling back to "False" would be exactly the mistake this field
    # exists to prevent (an untested case reported as confirmed-clean).
    log.warning(f"  unrecognized stock_mismatch value {raw!r} -- bucketed as Unknown, not False.")
    return "Unknown", s


# ---------------------------------------------------------------------------
# 5. Cross-collection SKU dedup (also resolves Home Centre's category-subset
#    issue as a side effect of the same generic mechanism, not a special case)
# ---------------------------------------------------------------------------
def dedupe_cross_collection(rows):
    """Groups by (brand, product_url). Keeps the most-recently-collected
    row's data as canonical (price, stock signals, collection_complete,
    etc.), but merges every distinct category the SKU was seen under into a
    `categories` column, so a product appearing under both a parent and
    child category (Home Centre Cookware/Bakeware) doesn't get silently
    single-tagged or double-counted. Returns (deduped_rows, rows_removed).
    """
    groups = defaultdict(list)
    for r in rows:
        groups[(r["brand"], r["product_url"])].append(r)

    deduped = []
    rows_removed = 0
    for (brand, url), group in groups.items():
        if len(group) > 1:
            rows_removed += len(group) - 1
        group.sort(key=lambda r: r["collected_at"])
        canonical = dict(group[-1])  # most recent wins for all point-in-time fields
        canonical["categories"] = ";".join(sorted(set(r["category"] for r in group)))
        deduped.append(canonical)

    return deduped, rows_removed


# ---------------------------------------------------------------------------
# 6. Per-(brand, category) completeness summary -- computed from the RAW
#    rows before cross-collection dedup, since collection_complete is a
#    fact about a specific collection run, not about a deduped SKU.
# ---------------------------------------------------------------------------
def build_completeness_summary(raw_rows):
    groups = defaultdict(list)
    for r in raw_rows:
        groups[(r["brand"], r["category"])].append(r)

    summary = []
    for (brand, category), group in sorted(groups.items()):
        unique_skus = len(set(r["product_url"] for r in group))
        all_complete = all(r["collection_complete"] == "True" for r in group)
        any_complete = any(r["collection_complete"] == "True" for r in group)
        if all_complete:
            status = "complete"
        elif any_complete:
            status = "mixed -- some collection runs complete, some partial"
        else:
            status = "partial"
        summary.append({
            "brand": brand,
            "category": category,
            "unique_sku_count": unique_skus,
            "raw_row_count": len(group),
            "collection_status": status,
        })
    return summary


# ---------------------------------------------------------------------------
# Row-level normalization (price parsing, mismatch bucketing) applied before
# dedup, so the canonical row picked by dedup already has clean fields.
# ---------------------------------------------------------------------------
def _normalize_row(r):
    price, price_flag = _parse_numeric(r.get("price"))
    compare_at_price, _ = _parse_numeric(r.get("compare_at_price"))
    discount_percent, _ = _parse_numeric(r.get("discount_percent"))
    mismatch_bucket, mismatch_detail = _bucket_stock_mismatch(r.get("stock_mismatch"))

    return {
        "brand": r["brand"],
        "category": r["category"],
        "product_name": r.get("product_name", ""),
        "product_url": r["product_url"],
        "price": price,
        "price_flag": price_flag or "",
        "price_raw": r.get("price", "") if price_flag else "",
        "compare_at_price": compare_at_price,
        "discount_percent": discount_percent,
        "material_type_tag": r.get("material_type_tag", ""),
        "stock_status_tag": r.get("stock_status_tag", ""),
        "actual_button_state": r.get("actual_button_state", ""),
        "stock_mismatch": mismatch_bucket,
        "stock_mismatch_detail": mismatch_detail,
        "collection_complete": r.get("collection_complete", ""),
        "source_type": r.get("source_type", ""),
        "collected_at": r.get("collected_at", ""),
    }


def main():
    raw_rows = _load_nestasia_rows() + _load_competitor_rows()
    if not raw_rows:
        log.error("No input rows loaded -- nothing to normalize.")
        return

    total_raw = len(raw_rows)

    # Completeness summary must be computed on the raw, pre-dedup rows --
    # see build_completeness_summary()'s docstring for why.
    summary = build_completeness_summary(raw_rows)

    material_merges = merge_duplicate_case_tags(raw_rows, "material_type_tag")

    normalized_rows = [_normalize_row(r) for r in raw_rows]
    deduped_rows, rows_removed = dedupe_cross_collection(normalized_rows)

    # Write outputs
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_FIELDNAMES)
        writer.writeheader()
        writer.writerows(deduped_rows)

    with open(SUMMARY_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["brand", "category", "unique_sku_count", "raw_row_count", "collection_status"])
        writer.writeheader()
        writer.writerows(summary)

    # ---- Report ----
    mismatch_counts = defaultdict(int)
    for r in deduped_rows:
        mismatch_counts[r["stock_mismatch"]] += 1

    price_flag_counts = defaultdict(int)
    for r in deduped_rows:
        if r["price_flag"]:
            price_flag_counts[r["price_flag"]] += 1

    print()
    print("=" * 70)
    print("normalize.py -- run report")
    print("=" * 70)
    print(f"Raw input rows loaded:                {total_raw}")
    print(f"Duplicate-case tags merged:            {material_merges}")
    print(f"Cross-collection duplicate rows removed: {rows_removed}")
    print(f"Final unique SKU rows:                 {len(deduped_rows)}")
    print()
    print("Per-(brand, category) completeness summary (from RAW rows, before dedup):")
    for s in summary:
        flag = "" if s["collection_status"] == "complete" else "  *** " + s["collection_status"].upper() + " ***"
        print(f"  {s['brand']} / {s['category']}: {s['unique_sku_count']} unique SKUs "
              f"({s['raw_row_count']} raw rows){flag}")
    print()
    print("stock_mismatch breakdown in final output (never collapsed to boolean):")
    for key in ("True", "False", "Unknown", "N/A"):
        print(f"  {key}: {mismatch_counts.get(key, 0)}")
    unexpected = set(mismatch_counts) - {"True", "False", "Unknown", "N/A"}
    if unexpected:
        print(f"  WARNING -- unexpected bucket(s) found: {unexpected}")
    print()
    if price_flag_counts:
        print("Price parsing issues flagged (NOT dropped, NOT guessed):")
        for flag, count in price_flag_counts.items():
            print(f"  {flag}: {count} row(s) -- see price_raw column in {OUTPUT_FILE}")
    else:
        print("No price parsing issues found.")
    print("=" * 70)


if __name__ == "__main__":
    main()
