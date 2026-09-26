"""
Independent verification of the paid_ad_creative load -- fresh script,
fresh connection, querying production directly. Does not import or trust
load_paid_ad_creative.py's own printed counts.
"""
import os

import psycopg2

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


def main():
    env = _load_env()
    conn = psycopg2.connect(
        host=env["PRODUCTION_DB_HOST"], port=env["PRODUCTION_DB_PORT"],
        dbname=env["PRODUCTION_DB_NAME"], user=env["PRODUCTION_DB_USER"],
        password=env["PRODUCTION_DB_PASSWORD"], sslmode="require", connect_timeout=15,
    )
    cur = conn.cursor()

    print("=" * 70)
    print("1. Total row count in paid_ad_creative")
    print("=" * 70)
    cur.execute("SELECT count(*) FROM paid_ad_creative")
    print(f"SELECT count(*) FROM paid_ad_creative;\n-> {cur.fetchone()[0]}")

    print()
    print("=" * 70)
    print("2. Breakdown by brand")
    print("=" * 70)
    cur.execute(
        """
        SELECT COALESCE(b.name, cb.name) AS brand_name, count(*)
        FROM paid_ad_creative pac
        LEFT JOIN brand b ON b.id = pac.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
        GROUP BY 1 ORDER BY 1
        """
    )
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")

    print()
    print("=" * 70)
    print("3. source_record linkage: every row has a valid FK with source_type='ad_library_public'")
    print("=" * 70)
    cur.execute(
        """
        SELECT count(*), count(*) FILTER (WHERE sr.source_type = 'ad_library_public'), count(*) FILTER (WHERE sr.id IS NULL)
        FROM paid_ad_creative pac
        LEFT JOIN source_record sr ON sr.id = pac.source_record_id
        """
    )
    total, correct_type, missing_fk = cur.fetchone()
    print(f"total rows={total}, with source_type='ad_library_public'={correct_type}, missing source_record FK={missing_fk}")

    print()
    print("=" * 70)
    print("4. Spot-check 3 rows against the spreadsheet by hand")
    print("=" * 70)
    cur.execute(
        """
        SELECT COALESCE(b.name, cb.name), pac.ad_library_url, pac.start_date, pac.days_running,
               pac.ad_format, pac.product_subcategory, pac.hook_headline, pac.offer_discount,
               pac.cta_button, pac.landing_page_url, pac.notes, pac.theme, sr.source_type, sr.collected_at
        FROM paid_ad_creative pac
        LEFT JOIN brand b ON b.id = pac.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
        JOIN source_record sr ON sr.id = pac.source_record_id
        WHERE pac.ad_library_url IN (
            'https://www.facebook.com/ads/library/?id=1889587339150057',
            'https://www.facebook.com/ads/library/?id=1667768090990525',
            'https://www.facebook.com/ads/library/?id=1383447017086076'
        )
        ORDER BY pac.ad_library_url
        """
    )
    cols = ["brand", "ad_library_url", "start_date", "days_running", "ad_format", "product_subcategory",
            "hook_headline", "offer_discount", "cta_button", "landing_page_url", "notes", "theme",
            "source_type", "collected_at"]
    for row in cur.fetchall():
        print()
        for c, v in zip(cols, row):
            print(f"  {c:<20} {v!r}")

    print()
    print("=" * 70)
    print("Extra: newly-created competitor_brand rows")
    print("=" * 70)
    cur.execute("SELECT id, name, platform, notes FROM competitor_brand WHERE name IN ('Borosil', 'IKEA India')")
    for row in cur.fetchall():
        print(f"  id={row[0]} name={row[1]!r} platform={row[2]!r} notes={row[3]!r}")

    conn.close()


if __name__ == "__main__":
    main()
