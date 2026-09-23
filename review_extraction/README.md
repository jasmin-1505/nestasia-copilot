# Kitchen Category Review Extraction — Setup & Usage

## What this does (discovery, then extraction)
Discovery is two interchangeable scripts — same input (`brand_categories.csv`),
same output format (`products.csv`), different fetch mechanism:
1. **`discover_products.py`** (plain `requests` + BeautifulSoup) — crawls a
   brand's category/listing page, paginates through it, and auto-fills
   `products.csv` with every product URL + name it finds. Works fine for
   `search?q=...` listing URLs; returns HTTP 500 for `~brand/pr` brand-store
   URLs (that pattern is broken on Flipkart's end — see NOTES.md "Broken
   brand-store URL pattern"). May also get IP-blocked depending on the
   network it runs from — see NOTES.md "IP-reputation-based blocking".
2. **`discover_products_playwright.py`** (headless Chromium via Playwright)
   — same crawl/paginate/output logic, but drives a real browser instead of
   raw HTTP requests, which sidesteps request-header-based blocking. It does
   **not** fix the `~brand/pr` 500s, since that's a dead URL, not a blocking
   measure — use `search?q=` URLs regardless of which discovery script you run.
   Currently Flipkart-only (Amazon isn't wired up in this version).

Then:
3. **`extract_reviews.py`** — reads `products.csv` and pulls reviews for
   each product, appending only NEW reviews on every run (incremental,
   not real-time — see NOTES.md).

## ⚠️ Read this first — two known issues, don't conflate them
1. **Brand-store URLs (`~brand/pr`) return HTTP 500** on Flipkart, confirmed
   with both discovery scripts. This is a dead URL pattern, unrelated to
   blocking. Use `search?q=<brand>+cookware` URLs in `brand_categories.csv`
   instead — see NOTES.md "Broken brand-store URL pattern".
2. **IP-reputation-based blocking is possible but not guaranteed.** One
   tested session from a cloud sandbox saw Flipkart's homepage return HTTP
   403; a later session, plain `requests` calls succeeded (HTTP 200) from
   that session's network. Whether you get blocked depends on the network
   you're actually running from — test it rather than assuming either way.
   `discover_products_playwright.py` (real browser traffic) is less likely
   to trip header-based blocking than plain `requests`, but isn't guaranteed
   to bypass IP-reputation blocking either. Full detail in NOTES.md.

## 1. Install
```bash
pip install requests beautifulsoup4 python-dateutil
```
If you plan to use the Playwright-based discovery script (recommended —
uses real browser traffic instead of raw HTTP requests):
```bash
pip install playwright
playwright install chromium
```

## 2. Discover products automatically
Edit `brand_categories.csv` — ONE row per brand+platform, pointing to a
**search results** listing page for that brand (not a brand-store page —
see the warning above):
```
brand,platform,category_url
Nestasia,flipkart,https://www.flipkart.com/search?q=nestasia+cookware
Prestige,flipkart,https://www.flipkart.com/search?q=prestige+cookware
```
Then run one of the two discovery scripts:
```bash
# Plain requests + BeautifulSoup -- simpler, faster, but more exposed to
# header/request-based blocking:
python3 discover_products.py

# Playwright (headless Chromium) -- same output, drives a real browser
# instead of raw HTTP requests:
python3 discover_products_playwright.py
```
Both crawl each listing page (with pagination) and write the discovered
products into `products.csv` in the same format — check that file
afterward to see what was found, and prune anything irrelevant before the
next step.

## 3. Extract reviews
```bash
python3 extract_reviews.py
```
First run: pulls full available review history for every product now in
`products.csv` (populated by step 2).
Every run after that: only fetches reviews newer than the last checkpoint
per product — cheap, fast, no duplicates.

Output accumulates in `reviews_dataset.csv` — this is the file you hand to
the knowledge-base build step (Week 3 of the proposal timeline).

## 4. Schedule it (optional — makes it "refresh automatically")
Run BOTH scripts in sequence on your chosen cadence, entirely under your
control — nothing in either script is time-locked:

**Linux/Mac (cron)** — runs every day at 3am (using the Playwright discovery
script — see warning above on why that's the safer default):
```bash
crontab -e
# add this line:
0 3 * * * cd /path/to/review_extraction && /usr/bin/python3 discover_products_playwright.py && /usr/bin/python3 extract_reviews.py >> run.log 2>&1
```

**Windows (Task Scheduler)**: create a Basic Task → Daily → Action: Start a
Program → `python.exe` → Arguments: pointing to a small `.bat` file that
runs both scripts in sequence → Start in: this folder.

Change `0 3 * * *` to `0 3 * * 1` for weekly-on-Monday if that better
matches the proposal's "weekly refresh" language. See the two-issues note
above before assuming this will run unattended on a cloud scheduler.

## 5. Rate limits and etiquette
- `REQUEST_DELAY_SECONDS` (3s) in `discover_products.py` and
  `extract_reviews.py` spaces out requests between pages/products, per the
  proposal's "respecting each site's terms and rate limits."
- `discover_products_playwright.py` uses a differently-named constant for
  the same purpose: `DELAY_BETWEEN_PAGES_SECONDS` (9s) — it is not the same
  variable as `REQUEST_DELAY_SECONDS`, don't assume changing one changes
  the other. It also caps pagination lower, `MAX_PAGES_PER_CATEGORY = 6`
  (vs. 15 in `discover_products.py`) — deep result pages are low-relevance
  anyway, and fewer pages means fewer requests per run.
- Amazon.in's anti-bot measures are stronger than Flipkart's, and Flipkart
  itself may block cloud IPs depending on the network (see the two-issues
  note above — this is not guaranteed to happen). For
  Amazon specifically, the proposal's suggested "manual fallback
  collection" (per the risk table) is often more reliable than scripted
  collection — see NOTES.md.
- Never run this at a frequency higher than daily. There is no analytical
  value in more-frequent polling, and it materially raises block risk.

## Competitor storefront SKU collection
`competitor_collector.py` collects live catalogue data (price, discount,
stock status, stock-mismatch detection) directly from competitor brands' own
websites, following the same conventions as `own_site_collector.py`:
config-driven CSV input, block/interstitial detection on every page load,
`collection_complete` flag, `source_type="live_storefront"`, `collected_at`
timestamp. Unlike `own_site_collector.py`, each competitor brand runs on a
different platform, so both card selectors AND pagination mechanics are
dispatched per row by a `platform` column in `competitor_sites.csv`.
Implemented so far: `shopify_t4s` (Wonderchef), `unbxd_nextjs` (Home
Centre — Next.js + Material-UI on an Unbxd search API, click-triggered
pagination intercepted over the network rather than a URL query param), and
`shopify_hyper_sections` (Milton — Shopify's "Hyper" theme, pagination via
Shopify's Section Rendering API). **Prestige and Borosil are not yet in
`competitor_sites.csv`** — each needs its own live DOM reconnaissance (real
category URL, confirmed platform, confirmed card selector) before being
added; see NOTES.md findings (h), (i), and (l) for what that involved for
each brand built so far. Home Centre's category-browse API filters
out-of-stock SKUs out of the listing entirely and its grid cards have no
add-to-cart control — so its rows record `stock_mismatch` as `"N/A"` rather
than `True`/`False`; see finding (i) before treating that as a comparable
result to nestasia.in's confirmed stock-display bug. **Milton's rows, by
contrast, are the first competitor data where this check produced a real
result: 6 confirmed stock-display mismatches, verified against product-page
ground truth — see finding (l) for why this means the bug is not specific
to nestasia.in.**

```bash
python competitor_collector.py
```
Reads `competitor_sites.csv` (one row per brand+category+collection-URL),
appends a fresh snapshot to `competitor_catalogue.csv` — non-incremental,
same point-in-time rationale as `own_site_collector.py`.

## Cleaning the collected data
`normalize.py` merges `nestasia_catalogue.csv` and `competitor_catalogue.csv`
into one deduplicated dataset. Every rule it applies traces to something a
real collection run surfaced (see its module docstring and NOTES.md), not a
generic cleanup pass — in particular it never collapses `stock_mismatch`'s
`True`/`False`/`"Unknown"`/`"N/A"` distinction into a boolean, never drops a
row with a missing/unparseable price (flags it instead), and never silently
merges `collection_complete=False` rows into a clean-looking total.

```bash
python normalize.py
```
Reads both catalogue CSVs, writes `normalized_catalogue.csv` (one row per
unique SKU, deduplicated across brand+product_url so re-run duplicates and
Home Centre's Cookware/Bakeware taxonomy overlap don't double-count) and
`normalize_summary.csv` (one row per brand+category, with SKU counts and
whether that category's collection is complete or partial). Prints a full
report of what it changed — duplicate tags merged, cross-collection rows
removed, the stock_mismatch breakdown — every run, not just on request.

## Files
- `discover_products.py` — auto-finds product URLs from a category listing page (plain `requests`; works on `search?q=` URLs, 500s on `~brand/pr` URLs, may be IP-blocked depending on network)
- `discover_products_playwright.py` — same discovery logic via headless Chromium instead of raw HTTP requests; same output format, same `~brand/pr` limitation, less exposed to request-header-based blocking
- `extract_reviews.py` — pulls reviews for products in products.csv
- `own_site_collector.py` — Playwright SKU/price/stock collector for nestasia.in itself; see NOTES.md for its block-detection and stock-mismatch findings
- `competitor_collector.py` — same conventions, for competitor brand storefronts; platform-dispatched extraction, currently Wonderchef-only (`shopify_t4s`)
- `brand_categories.csv` — your input: one `search?q=` listing-page URL per brand (edit this)
- `nestasia_subcategories.csv` — input for `own_site_collector.py`: one row per nestasia.in subcategory+collection-URL
- `competitor_sites.csv` — input for `competitor_collector.py`: one row per brand+category+collection-URL+platform (Wonderchef only so far — see above)
- `products.csv` — auto-generated by whichever discovery script you run (or edit manually if you prefer)
- `checkpoint.json` — auto-generated, tracks what's already been collected
- `reviews_dataset.csv` — auto-generated, the accumulating output dataset
- `nestasia_catalogue.csv` — auto-generated by `own_site_collector.py`
- `competitor_catalogue.csv` — auto-generated by `competitor_collector.py`
- `normalize.py` — merges and deduplicates both catalogues into one cleaned dataset
- `normalized_catalogue.csv` — auto-generated by `normalize.py`, one row per unique SKU
- `normalize_summary.csv` — auto-generated by `normalize.py`, per brand+category SKU counts and completeness status
- `run.log` — auto-generated if scheduled, records each run's summary
