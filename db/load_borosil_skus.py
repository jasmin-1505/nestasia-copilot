"""
Loads ONLY the Borosil rows from review_extraction/normalized_catalogue.csv
into PRODUCTION -- NOT the full 1017-row file (load_production.py already
loaded the other 954; re-running it wholesale would hit sku.product_url's
UNIQUE constraint on every one of those). Fixture is never touched.

Reuses the EXISTING competitor_brand row for Borosil (created during the
paid-ad-creative load, id=5 as of this writing) rather than creating a
second one -- looked up by name, not assumed by id, and the script aborts
if no existing row is found rather than silently creating one, since a
silent create here would be exactly the duplicate-brand-row mistake this
load is specifically meant to avoid.

Same conventions as load_production.py: stock_mismatch and price_flag
mapped via fixed dicts that raise/abort on anything unrecognized,
categories split into sku_category join rows, one shared source_record per
SKU+PriceHistory pair, subcategory_id left NULL (no finer-grained data
exists to load).

Pre-flight also checks that none of these product_urls are already present
in `sku` -- extra insurance against accidentally double-loading Borosil if
this script were run twice.

Usage:
    python db/load_borosil_skus.py
"""
import csv
import os
import sys

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
CSV_FILE = os.path.join(os.path.dirname(__file__), "..", "review_extraction", "normalized_catalogue.csv")

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
        all_rows = list(csv.DictReader(f))
    rows = [r for r in all_rows if r["brand"] == "Borosil"]
    print(f"Filtered {len(rows)} Borosil rows out of {len(all_rows)} total rows in {CSV_FILE}")
    if not rows:
        print("ABORTING -- no Borosil rows found in the CSV.", file=sys.stderr)
        sys.exit(1)

    conn = _connect(env, "PRODUCTION")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # ---- Pre-flight: source_type validity
            cur.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'source_type_enum'"
            )
            valid_source_types = {r[0] for r in cur.fetchall()}
            csv_source_types = {r["source_type"] for r in rows}
            invalid = csv_source_types - valid_source_types
            if invalid:
                print(f"ABORTING -- invalid source_type value(s): {invalid}", file=sys.stderr)
                conn.rollback()
                sys.exit(1)
            print(f"Pre-flight OK -- source_type values valid: {sorted(csv_source_types)}")

            # ---- Pre-flight: reuse the EXISTING Borosil competitor_brand row.
            # Do NOT create one if missing -- abort instead, since a silent
            # create here is exactly the duplication this load must avoid.
            cur.execute("SELECT id FROM competitor_brand WHERE name = 'Borosil'")
            row = cur.fetchone()
            if row is None:
                print("ABORTING -- no existing competitor_brand row named 'Borosil' found. "
                      "Expected the one created during the paid-ad-creative load; refusing to "
                      "create a new one here.", file=sys.stderr)
                conn.rollback()
                sys.exit(1)
            borosil_id = row[0]
            print(f"Reusing existing competitor_brand row: Borosil (id={borosil_id})")

            # ---- Pre-flight: none of these product_urls should already exist
            urls = [r["product_url"] for r in rows]
            cur.execute("SELECT product_url FROM sku WHERE product_url = ANY(%s)", (urls,))
            already_present = [r[0] for r in cur.fetchall()]
            if already_present:
                print(f"ABORTING -- {len(already_present)} of these product_urls already exist in "
                      f"sku (would violate the UNIQUE constraint / indicate a double-load): "
                      f"{already_present[:5]}{'...' if len(already_present) > 5 else ''}", file=sys.stderr)
                conn.rollback()
                sys.exit(1)
            print("Pre-flight OK -- none of these product_urls already exist in sku.")

            # ---- category (across every value in `categories`, creating
            # only if genuinely new -- Cookware already exists from earlier loads)
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
                    print(f"ABORTING -- unmapped stock_mismatch {r['stock_mismatch']!r} on {r['product_url']}",
                          file=sys.stderr)
                    conn.rollback()
                    sys.exit(1)
                if r["price_flag"] not in PRICE_FLAG_MAP:
                    print(f"ABORTING -- unmapped price_flag {r['price_flag']!r} on {r['product_url']}",
                          file=sys.stderr)
                    conn.rollback()
                    sys.exit(1)

                cur.execute(
                    "INSERT INTO source_record (source_type, source_url, collected_at) "
                    "VALUES (%s, %s, %s) RETURNING id",
                    (r["source_type"], r["product_url"], r["collected_at"])
                )
                source_record_id = cur.fetchone()[0]

                cur.execute(
                    """
                    INSERT INTO sku (
                        brand_id, competitor_brand_id, subcategory_id,
                        product_name, product_url, material_type_tag,
                        stock_status_tag, actual_button_state,
                        stock_mismatch, collection_complete, source_record_id
                    ) VALUES (NULL, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        borosil_id,
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
                  f"{sku_count} price_history rows, {sku_count} source_record rows -- "
                  f"all linked to existing competitor_brand id={borosil_id} (Borosil).")

        conn.commit()
        print("COMMITTED.")
    except Exception:
        conn.rollback()
        print("ROLLED BACK -- no partial data left in production.", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
