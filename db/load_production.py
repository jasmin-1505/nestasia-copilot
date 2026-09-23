"""
Loads review_extraction/normalized_catalogue.csv (954 rows, from
normalize.py) into the PRODUCTION database only. Never touches fixture --
it stays empty until synthetic test data is deliberately generated for it
in a later pass.

Mapping decisions, and why:
  - `categories` (e.g. "Bakeware;Cookware") is split on ';' and inserted as
    multiple sku_category join rows -- exactly the case that join table was
    built for, not left as a delimited string in a single column.
  - stock_mismatch's four CSV values (True/False/N/A/Unknown, as Python's
    str(bool) capitalization) map to the schema's four enum labels
    (true/false/N/A/Unknown) with a fixed dict -- never coerced, never
    defaulted, and the mapping raises if it sees anything else rather than
    silently passing an unmapped value through.
  - price_flag: CSV empty string ("" -- normalize.py's own convention for
    "parsed successfully, nothing to flag") maps to the enum value
    'parsed'; CSV "empty" and "unparseable" pass through unchanged. The 10
    Prestige rows with price_flag="empty" get price=NULL, price_flag=
    'empty', and whatever normalize.py preserved in price_raw (empty
    string, in this case, since the ORIGINAL source field was genuinely
    blank -- there is nothing to preserve, which is different from a
    malformed-but-present value).
  - Every SKU and its corresponding PriceHistory row share ONE
    source_record, not two independent ones -- both were produced by the
    exact same collection event in the exact same CSV row, so a second,
    identical source_record would be redundant, not more correct. Every
    SKU/PriceHistory row still has a mandatory, non-null FK to it, which is
    the actual requirement.
  - subcategory_id is left NULL on every row: normalized_catalogue.csv's
    `category` column already IS own_site_collector.py's/competitor_
    collector.py's finest available grain (nestasia_subcategories.csv's
    per-URL split, e.g. jars-canisters vs fridge-storage-containers, was
    already collapsed to the single label "Container" before normalize.py
    ever ran) -- there is no real subcategory-level data in this CSV to
    load without fabricating it, so none is fabricated.

Pre-flight, BEFORE any insert: every source_type value actually present in
the CSV is checked against production's real source_type_enum (queried
live from pg_enum, not hardcoded) -- if anything doesn't match, this script
aborts with no rows written, per instructions, rather than forcing the
insert or silently defaulting.

The whole load runs in one transaction -- committed only if every row
loads cleanly, rolled back entirely on any failure. Usage:
    python db/load_production.py
"""

import csv
import os
import sys

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
CSV_FILE = os.path.join(os.path.dirname(__file__), "..", "review_extraction", "normalized_catalogue.csv")

COMPETITOR_PLATFORMS = {
    "Wonderchef": "shopify_t4s",
    "Home Centre": "unbxd_nextjs",
    "Milton": "shopify_hyper_sections",
    "Prestige": "magento_luma",
}

STOCK_MISMATCH_MAP = {
    "True": "true",
    "False": "false",
    "N/A": "N/A",
    "Unknown": "Unknown",
}

PRICE_FLAG_MAP = {
    "": "parsed",
    "empty": "empty",
    "unparseable": "unparseable",
}


def _load_env():
    values = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip()
    return values


def _connect(env, prefix):
    return psycopg2.connect(
        host=env[f"{prefix}_DB_HOST"], port=env[f"{prefix}_DB_PORT"],
        dbname=env[f"{prefix}_DB_NAME"], user=env[f"{prefix}_DB_USER"],
        password=env[f"{prefix}_DB_PASSWORD"], sslmode="require", connect_timeout=15,
    )


def _num_or_none(s):
    return float(s) if s not in (None, "") else None


def _bool(s):
    return s == "True"


def main():
    env = _load_env()
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} rows from {CSV_FILE}")

    conn = _connect(env, "PRODUCTION")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # ---- Pre-flight: every source_type in the CSV must already be
            # a valid production enum label. Abort before writing anything
            # if not.
            cur.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'source_type_enum'"
            )
            valid_source_types = {r[0] for r in cur.fetchall()}
            csv_source_types = {r["source_type"] for r in rows}
            invalid = csv_source_types - valid_source_types
            if invalid:
                print(f"ABORTING -- source_type value(s) in the CSV are not valid production enum "
                      f"labels, refusing to force or default them: {invalid}", file=sys.stderr)
                print(f"Valid production source_type values: {sorted(valid_source_types)}", file=sys.stderr)
                conn.rollback()
                sys.exit(1)
            print(f"Pre-flight OK -- all source_type values in the CSV are valid: {sorted(csv_source_types)}")

            # ---- brand / competitor_brand
            brand_ids = {}
            competitor_brand_ids = {}
            for brand_name in sorted(set(r["brand"] for r in rows)):
                if brand_name == "Nestasia":
                    cur.execute(
                        "INSERT INTO brand (name) VALUES (%s) "
                        "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                        "RETURNING id", (brand_name,)
                    )
                    brand_ids[brand_name] = cur.fetchone()[0]
                else:
                    cur.execute(
                        "INSERT INTO competitor_brand (name, platform) VALUES (%s, %s) "
                        "ON CONFLICT (name) DO UPDATE SET platform = EXCLUDED.platform "
                        "RETURNING id",
                        (brand_name, COMPETITOR_PLATFORMS.get(brand_name))
                    )
                    competitor_brand_ids[brand_name] = cur.fetchone()[0]
            print(f"brand rows: {brand_ids}")
            print(f"competitor_brand rows: {competitor_brand_ids}")

            # ---- category (across every value that appears in `categories`,
            # not just the primary `category` column)
            all_category_names = set()
            for r in rows:
                all_category_names.update(r["categories"].split(";"))
            category_ids = {}
            for name in sorted(all_category_names):
                cur.execute(
                    "INSERT INTO category (name) VALUES (%s) "
                    "ON CONFLICT (name) DO UPDATE SET name = EXCLUDED.name "
                    "RETURNING id", (name,)
                )
                category_ids[name] = cur.fetchone()[0]
            print(f"category rows: {category_ids}")

            # ---- SKU + source_record + price_history + sku_category
            sku_count = 0
            sku_category_count = 0
            for r in rows:
                if r["stock_mismatch"] not in STOCK_MISMATCH_MAP:
                    print(f"ABORTING -- unmapped stock_mismatch value {r['stock_mismatch']!r} "
                          f"on {r['product_url']}", file=sys.stderr)
                    conn.rollback()
                    sys.exit(1)
                if r["price_flag"] not in PRICE_FLAG_MAP:
                    print(f"ABORTING -- unmapped price_flag value {r['price_flag']!r} "
                          f"on {r['product_url']}", file=sys.stderr)
                    conn.rollback()
                    sys.exit(1)

                cur.execute(
                    "INSERT INTO source_record (source_type, source_url, collected_at) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (r["source_type"], r["product_url"], r["collected_at"])
                )
                source_record_id = cur.fetchone()[0]

                brand_id = brand_ids.get(r["brand"])
                competitor_brand_id = competitor_brand_ids.get(r["brand"])

                cur.execute(
                    """
                    INSERT INTO sku (
                        brand_id, competitor_brand_id, subcategory_id,
                        product_name, product_url, material_type_tag,
                        stock_status_tag, actual_button_state,
                        stock_mismatch, collection_complete, source_record_id
                    ) VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        brand_id, competitor_brand_id,
                        r["product_name"], r["product_url"],
                        r["material_type_tag"] or None,
                        r["stock_status_tag"] or None,
                        r["actual_button_state"] or None,
                        STOCK_MISMATCH_MAP[r["stock_mismatch"]],
                        _bool(r["collection_complete"]),
                        source_record_id,
                    )
                )
                sku_id = cur.fetchone()[0]
                sku_count += 1

                for cat_name in r["categories"].split(";"):
                    cur.execute(
                        "INSERT INTO sku_category (sku_id, category_id) VALUES (%s, %s) "
                        "ON CONFLICT DO NOTHING",
                        (sku_id, category_ids[cat_name])
                    )
                    sku_category_count += 1

                cur.execute(
                    """
                    INSERT INTO price_history (
                        sku_id, price, price_flag, price_raw,
                        compare_at_price, discount_percent, collected_at, source_record_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sku_id,
                        _num_or_none(r["price"]) if r["price_flag"] == "" else None,
                        PRICE_FLAG_MAP[r["price_flag"]],
                        r["price_raw"] or None,
                        _num_or_none(r["compare_at_price"]),
                        _num_or_none(r["discount_percent"]),
                        r["collected_at"],
                        source_record_id,
                    )
                )

            print(f"Inserted {sku_count} sku rows, {sku_category_count} sku_category rows, "
                  f"{sku_count} price_history rows, {sku_count} source_record rows.")

        conn.commit()
        print("COMMITTED.")
    except Exception:
        conn.rollback()
        print("ROLLED BACK due to error above -- no partial data was left in production.", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
