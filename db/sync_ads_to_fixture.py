"""
Copies paid_ad_creative (real, source_type='ad_library_public') from
PRODUCTION into FIXTURE -- READ-ONLY against production, write-only
against fixture, never the other direction. Same pattern as
sync_catalogue_to_fixture.py.

This was missed by that earlier sync: fixture got brand/competitor_brand/
category/sku/sku_category/price_history/source_record, but never
paid_ad_creative, so questions that need real ad data (Q16/Q20 in
business_questions_25.md) were silently running against zero ad rows in
fixture. This script closes that gap.

Ad rows keep their ORIGINAL source_type ('ad_library_public') -- they are
real, already-public ad-library data, not synthetic, exactly like sku/
price_history rows keep their original source_type when copied.

Each ad row gets its OWN source_record copied (not reused from the
catalogue sync's source_record_id_map), since paid_ad_creative.
source_record_id references rows that may not have been copied by the
catalogue sync at all.

Usage:
    python db/sync_ads_to_fixture.py
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

        fix_cur.execute("SELECT count(*) FROM paid_ad_creative")
        if fix_cur.fetchone()[0] > 0:
            raise SystemExit("ABORTING -- fixture already has paid_ad_creative rows; "
                              "this script is meant to run once. Not re-syncing.")

        # Resolve fixture-side brand/competitor_brand ids by name, since ids
        # are independent per-database (same approach as the catalogue sync).
        fix_cur.execute("SELECT id, name FROM brand")
        fix_brand_by_name = {name: bid for bid, name in fix_cur.fetchall()}
        fix_cur.execute("SELECT id, name FROM competitor_brand")
        fix_competitor_by_name = {name: cid for cid, name in fix_cur.fetchall()}

        prod_cur.execute("SELECT id, name FROM brand")
        prod_brand_names = {r["id"]: r["name"] for r in prod_cur.fetchall()}
        prod_cur.execute("SELECT id, name FROM competitor_brand")
        prod_competitor_names = {r["id"]: r["name"] for r in prod_cur.fetchall()}

        prod_cur.execute(
            """
            SELECT pac.id, pac.brand_id, pac.competitor_brand_id, pac.platform, pac.ad_library_url,
                   pac.start_date, pac.status, pac.end_date, pac.days_running, pac.ad_format,
                   pac.product_subcategory, pac.hook_headline, pac.offer_discount, pac.cta_button,
                   pac.landing_page_url, pac.notes, pac.theme,
                   sr.source_type::text AS source_type, sr.source_url, sr.collected_at, sr.notes AS sr_notes
            FROM paid_ad_creative pac
            JOIN source_record sr ON sr.id = pac.source_record_id
            """
        )
        rows = prod_cur.fetchall()

        ad_count = 0
        for row in rows:
            fix_brand_id = fix_brand_by_name[prod_brand_names[row["brand_id"]]] if row["brand_id"] else None
            fix_competitor_id = (
                fix_competitor_by_name[prod_competitor_names[row["competitor_brand_id"]]]
                if row["competitor_brand_id"] else None
            )
            fix_cur.execute(
                "INSERT INTO source_record (source_type, source_url, collected_at, notes) VALUES (%s, %s, %s, %s) RETURNING id",
                (row["source_type"], row["source_url"], row["collected_at"], row["sr_notes"]),
            )
            fix_source_record_id = fix_cur.fetchone()[0]

            fix_cur.execute(
                """
                INSERT INTO paid_ad_creative (
                    brand_id, competitor_brand_id, platform, ad_library_url, start_date, status,
                    end_date, days_running, ad_format, product_subcategory, hook_headline,
                    offer_discount, cta_button, landing_page_url, notes, theme, source_record_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    fix_brand_id, fix_competitor_id, row["platform"], row["ad_library_url"],
                    row["start_date"], row["status"], row["end_date"], row["days_running"],
                    row["ad_format"], row["product_subcategory"], row["hook_headline"],
                    row["offer_discount"], row["cta_button"], row["landing_page_url"],
                    row["notes"], row["theme"], fix_source_record_id,
                ),
            )
            ad_count += 1

        fix.commit()
        print(f"paid_ad_creative: synced {ad_count} row(s)")
        print("COMMITTED.")
    except Exception:
        fix.rollback()
        print("ROLLED BACK -- no partial data left in fixture.")
        raise
    finally:
        prod.close()
        fix.close()


if __name__ == "__main__":
    sync()
