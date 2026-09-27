"""
Independent verification of the synthetic business-data load -- fresh
script, fresh connections to BOTH databases, no reliance on
load_synthetic_business_data.py's own printed counts.

Confirms:
  1. Production's 5 new tables are still exactly 0 rows (nothing was ever
     written there).
  2. Fixture's row counts per table.
  3. Every new-table row has is_synthetic=true AND links to a
     source_record with source_type='synthetic' -- both signals, not one.
  4. Spot-check a handful of rows by hand.
"""
import os

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
NEW_TABLES = ["sales_data", "inventory_data", "channel_performance", "margin_data", "social_engagement"]


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


def main():
    env = _load_env()

    print("=" * 70)
    print("1. PRODUCTION -- must be exactly 0 for all 5 new tables")
    print("=" * 70)
    prod = _connect(env, "PRODUCTION")
    cur = prod.cursor()
    for t in NEW_TABLES:
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t}: {cur.fetchone()[0]}")
    prod.close()

    print()
    print("=" * 70)
    print("2. FIXTURE -- row counts")
    print("=" * 70)
    fix = _connect(env, "FIXTURE")
    cur = fix.cursor()
    for t in NEW_TABLES:
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t}: {cur.fetchone()[0]}")

    print()
    print("=" * 70)
    print("3. is_synthetic=true AND source_type='synthetic' on every row -- both signals")
    print("=" * 70)
    for t in NEW_TABLES:
        cur.execute(
            f"""
            SELECT count(*),
                   count(*) FILTER (WHERE t.is_synthetic = true),
                   count(*) FILTER (WHERE sr.source_type = 'synthetic')
            FROM {t} t JOIN source_record sr ON sr.id = t.source_record_id
            """
        )
        total, is_synth, src_synth = cur.fetchone()
        print(f"  {t}: total={total} is_synthetic=true:{is_synth} source_type=synthetic:{src_synth}")

    print()
    print("=" * 70)
    print("4. Spot-check: 2 sales_data rows, 1 margin_data row, 1 social_engagement row")
    print("=" * 70)
    cur.execute(
        """
        SELECT s.product_name, sd.period_start, sd.period_end, sd.units_sold, sd.revenue, sd.is_synthetic
        FROM sales_data sd JOIN sku s ON s.id = sd.sku_id
        ORDER BY sd.id LIMIT 2
        """
    )
    for row in cur.fetchall():
        print(" ", row)

    cur.execute(
        """
        SELECT s.product_name, ph.price, md.unit_cost, md.margin_percent
        FROM margin_data md JOIN sku s ON s.id = md.sku_id
        JOIN price_history ph ON ph.sku_id = s.id
        ORDER BY md.id LIMIT 1
        """
    )
    print(" ", cur.fetchone())

    cur.execute(
        """
        SELECT COALESCE(b.name, cb.name), se.post_date, se.platform, se.likes, se.comments, se.shares
        FROM social_engagement se
        LEFT JOIN brand b ON b.id = se.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = se.competitor_brand_id
        ORDER BY se.id LIMIT 3
        """
    )
    for row in cur.fetchall():
        print(" ", row)

    print()
    print("=" * 70)
    print("5. Confirm the 10 unpriced SKUs were correctly skipped for sales/margin/channel")
    print("=" * 70)
    cur.execute(
        """
        SELECT count(*) FROM sku s
        WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.sku_id = s.id AND ph.price IS NOT NULL)
        """
    )
    unpriced_in_fixture = cur.fetchone()[0]
    print(f"  SKUs in fixture with no real price: {unpriced_in_fixture}")
    cur.execute(
        """
        SELECT count(*) FROM sku s
        WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.sku_id = s.id AND ph.price IS NOT NULL)
        AND EXISTS (SELECT 1 FROM sales_data sd WHERE sd.sku_id = s.id)
        """
    )
    print(f"  Of those, how many got a sales_data row anyway (should be 0): {cur.fetchone()[0]}")
    cur.execute(
        """
        SELECT count(*) FROM sku s
        WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.sku_id = s.id AND ph.price IS NOT NULL)
        AND EXISTS (SELECT 1 FROM inventory_data id2 WHERE id2.sku_id = s.id)
        """
    )
    print(f"  Of those, how many got an inventory_data row anyway (should be 10, inventory doesn't need price): {cur.fetchone()[0]}")

    fix.close()


if __name__ == "__main__":
    main()
