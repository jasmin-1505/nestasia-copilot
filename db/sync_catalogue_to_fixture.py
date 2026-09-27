"""
Copies the real catalogue structure (brand, competitor_brand, category,
sku, sku_category, source_record, price_history) from PRODUCTION into
FIXTURE -- READ-ONLY against production, write-only against fixture, never
the other direction.

Why this is needed before loading any synthetic business data: fixture's
schema now has sales_data/inventory_data/channel_performance/margin_data
(migration 002), all with a mandatory sku_id FK, but fixture's own `sku`
table is empty -- it never received the real collection data the way
production did. Without copying the real SKUs (and their real brand/
category/price context) into fixture first, there is nothing for the new
synthetic tables to reference.

This does NOT make fixture's data synthetic-only anymore, and that's
intentional: a demo needs realistic product context (real names, URLs,
prices) alongside the made-up business numbers, and the whole
"synthetic" concern in this project has always been about not creating
FAKE catalogue/collection facts, not about fixture being forbidden from
holding a copy of real, already-public data. Copied rows keep their
ORIGINAL source_type (e.g. 'live_storefront') -- only the new business
tables loaded afterward get source_type='synthetic'.

IDs are NOT preserved across databases (each has its own independent
sequence) -- this script builds and returns a product_url -> fixture_sku_id
mapping so a later script can look up the right fixture-side ID.

Usage:
    python db/sync_catalogue_to_fixture.py
"""
import os

import psycopg2
import psycopg2.extras


ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")


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


def sync():
    env = _load_env()
    prod = _connect(env, "PRODUCTION")
    fix = _connect(env, "FIXTURE")
    fix.autocommit = False
    try:
        prod_cur = prod.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        fix_cur = fix.cursor()

        # ---- Refuse to double-sync. If fixture already has SKUs, this
        # script has run before -- abort rather than duplicate everything,
        # since re-running blindly would violate sku.product_url's UNIQUE
        # constraint anyway, but abort with a clear reason instead of a
        # raw constraint-violation traceback.
        fix_cur.execute("SELECT count(*) FROM sku")
        if fix_cur.fetchone()[0] > 0:
            raise SystemExit("ABORTING -- fixture already has sku rows; this script is meant to "
                              "run once against an empty fixture catalogue. Not re-syncing.")

        # ---- brand
        prod_cur.execute("SELECT name FROM brand")
        brand_id_map = {}
        for row in prod_cur.fetchall():
            fix_cur.execute("INSERT INTO brand (name) VALUES (%s) RETURNING id", (row["name"],))
            brand_id_map[row["name"]] = fix_cur.fetchone()[0]
        print(f"brand: synced {len(brand_id_map)} row(s)")

        # ---- competitor_brand
        prod_cur.execute("SELECT name, platform, base_url, notes FROM competitor_brand")
        competitor_brand_id_map = {}
        for row in prod_cur.fetchall():
            fix_cur.execute(
                "INSERT INTO competitor_brand (name, platform, base_url, notes) VALUES (%s, %s, %s, %s) RETURNING id",
                (row["name"], row["platform"], row["base_url"], row["notes"]),
            )
            competitor_brand_id_map[row["name"]] = fix_cur.fetchone()[0]
        print(f"competitor_brand: synced {len(competitor_brand_id_map)} row(s)")

        # ---- category
        prod_cur.execute("SELECT name FROM category")
        category_id_map = {}
        for row in prod_cur.fetchall():
            fix_cur.execute("INSERT INTO category (name) VALUES (%s) RETURNING id", (row["name"],))
            category_id_map[row["name"]] = fix_cur.fetchone()[0]
        print(f"category: synced {len(category_id_map)} row(s)")

        # ---- source_record (one-for-one copy, keeping original source_type)
        prod_cur.execute("SELECT id, source_type::text AS source_type, source_url, collected_at, notes FROM source_record")
        source_record_id_map = {}
        source_record_rows = prod_cur.fetchall()
        for row in source_record_rows:
            fix_cur.execute(
                "INSERT INTO source_record (source_type, source_url, collected_at, notes) VALUES (%s, %s, %s, %s) RETURNING id",
                (row["source_type"], row["source_url"], row["collected_at"], row["notes"]),
            )
            source_record_id_map[row["id"]] = fix_cur.fetchone()[0]
        print(f"source_record: synced {len(source_record_id_map)} row(s)")

        # ---- sku (need production-side id->name maps first, to re-resolve
        # brand/competitor_brand against fixture's own, independently
        # generated ids)
        sku_id_map = {}
        prod_cur.execute("SELECT id, name FROM brand")
        prod_brand_names = {r["id"]: r["name"] for r in prod_cur.fetchall()}
        prod_cur.execute("SELECT id, name FROM competitor_brand")
        prod_competitor_names = {r["id"]: r["name"] for r in prod_cur.fetchall()}

        prod_cur.execute(
            """
            SELECT id, brand_id, competitor_brand_id, product_name, product_url,
                   material_type_tag, stock_status_tag, actual_button_state,
                   stock_mismatch::text AS stock_mismatch, collection_complete, source_record_id
            FROM sku
            """
        )
        for row in prod_cur.fetchall():
            fix_brand_id = brand_id_map[prod_brand_names[row["brand_id"]]] if row["brand_id"] else None
            fix_competitor_id = (
                competitor_brand_id_map[prod_competitor_names[row["competitor_brand_id"]]]
                if row["competitor_brand_id"] else None
            )
            fix_cur.execute(
                """
                INSERT INTO sku (brand_id, competitor_brand_id, subcategory_id, product_name, product_url,
                                  material_type_tag, stock_status_tag, actual_button_state, stock_mismatch,
                                  collection_complete, source_record_id)
                VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    fix_brand_id, fix_competitor_id, row["product_name"], row["product_url"],
                    row["material_type_tag"], row["stock_status_tag"], row["actual_button_state"],
                    row["stock_mismatch"], row["collection_complete"],
                    source_record_id_map[row["source_record_id"]],
                ),
            )
            sku_id_map[row["id"]] = fix_cur.fetchone()[0]
        print(f"sku: synced {len(sku_id_map)} row(s)")

        # ---- sku_category
        prod_cur.execute("SELECT id, name FROM category")
        prod_category_names = {r["id"]: r["name"] for r in prod_cur.fetchall()}
        prod_cur.execute("SELECT sku_id, category_id FROM sku_category")
        sku_category_count = 0
        for row in prod_cur.fetchall():
            fix_cur.execute(
                "INSERT INTO sku_category (sku_id, category_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (sku_id_map[row["sku_id"]], category_id_map[prod_category_names[row["category_id"]]]),
            )
            sku_category_count += 1
        print(f"sku_category: synced {sku_category_count} row(s)")

        # ---- price_history (need its own source_record too -- reuse the
        # same mapping since price_history.source_record_id references the
        # same source_record table)
        prod_cur.execute(
            "SELECT sku_id, price, price_flag::text AS price_flag, price_raw, compare_at_price, "
            "discount_percent, collected_at, source_record_id FROM price_history"
        )
        price_history_count = 0
        for row in prod_cur.fetchall():
            fix_cur.execute(
                """
                INSERT INTO price_history (sku_id, price, price_flag, price_raw, compare_at_price,
                                            discount_percent, collected_at, source_record_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sku_id_map[row["sku_id"]], row["price"], row["price_flag"], row["price_raw"],
                    row["compare_at_price"], row["discount_percent"], row["collected_at"],
                    source_record_id_map[row["source_record_id"]],
                ),
            )
            price_history_count += 1
        print(f"price_history: synced {price_history_count} row(s)")

        fix.commit()
        print("\nCOMMITTED.")

        return {
            "brand_id_map": brand_id_map,
            "competitor_brand_id_map": competitor_brand_id_map,
            "sku_id_map": sku_id_map,
        }
    except Exception:
        fix.rollback()
        print("ROLLED BACK -- no partial data left in fixture.")
        raise
    finally:
        prod.close()
        fix.close()


if __name__ == "__main__":
    sync()
