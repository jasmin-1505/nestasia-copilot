"""
Independent verification of the production data load -- a fresh script,
fresh connection, querying production directly. Does not import or trust
anything from load_production.py's own success message.
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
    print("1. Total row count in sku")
    print("=" * 70)
    cur.execute("SELECT count(*) FROM sku")
    print(f"SELECT count(*) FROM sku;\n-> {cur.fetchone()[0]}")

    print()
    print("=" * 70)
    print("2. stock_mismatch breakdown")
    print("=" * 70)
    cur.execute("SELECT stock_mismatch, count(*) FROM sku GROUP BY stock_mismatch ORDER BY stock_mismatch")
    print("SELECT stock_mismatch, count(*) FROM sku GROUP BY stock_mismatch ORDER BY stock_mismatch;")
    for row in cur.fetchall():
        print(f"  {row[0]}: {row[1]}")

    print()
    print("=" * 70)
    print("3. collection_complete=False spot-check (nestasia.in Container)")
    print("=" * 70)
    cur.execute(
        """
        SELECT b.name, c.name, s.collection_complete, count(*)
        FROM sku s
        JOIN brand b ON b.id = s.brand_id
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        WHERE b.name = 'Nestasia' AND c.name = 'Container'
        GROUP BY b.name, c.name, s.collection_complete
        ORDER BY s.collection_complete
        """
    )
    print("SELECT b.name, c.name, s.collection_complete, count(*) FROM sku s "
          "JOIN brand b ON b.id=s.brand_id JOIN sku_category sc ON sc.sku_id=s.id "
          "JOIN category c ON c.id=sc.category_id WHERE b.name='Nestasia' AND c.name='Container' "
          "GROUP BY b.name, c.name, s.collection_complete ORDER BY s.collection_complete;")
    for row in cur.fetchall():
        print(f"  brand={row[0]} category={row[1]} collection_complete={row[2]} count={row[3]}")

    print()
    print("  Full collection_complete breakdown by brand+category, for context:")
    cur.execute(
        """
        SELECT COALESCE(b.name, cb.name), c.name, s.collection_complete, count(*)
        FROM sku s
        LEFT JOIN brand b ON b.id = s.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = s.competitor_brand_id
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        GROUP BY 1, 2, s.collection_complete
        ORDER BY 1, 2
        """
    )
    for row in cur.fetchall():
        print(f"    {row[0]:<14} {row[1]:<22} collection_complete={row[2]!s:<5} count={row[3]}")

    print()
    print("=" * 70)
    print("4. Prestige empty-price rows: price IS NULL AND price_flag = 'empty'")
    print("=" * 70)
    cur.execute(
        """
        SELECT count(*)
        FROM price_history ph
        JOIN sku s ON s.id = ph.sku_id
        JOIN competitor_brand cb ON cb.id = s.competitor_brand_id
        WHERE cb.name = 'Prestige' AND ph.price IS NULL AND ph.price_flag = 'empty'
        """
    )
    print("SELECT count(*) FROM price_history ph JOIN sku s ON s.id=ph.sku_id "
          "JOIN competitor_brand cb ON cb.id=s.competitor_brand_id "
          "WHERE cb.name='Prestige' AND ph.price IS NULL AND ph.price_flag='empty';")
    print(f"-> {cur.fetchone()[0]}")

    print()
    print("  Sample rows (product_url, price, price_flag, price_raw):")
    cur.execute(
        """
        SELECT s.product_url, ph.price, ph.price_flag, ph.price_raw
        FROM price_history ph
        JOIN sku s ON s.id = ph.sku_id
        JOIN competitor_brand cb ON cb.id = s.competitor_brand_id
        WHERE cb.name = 'Prestige' AND ph.price IS NULL AND ph.price_flag = 'empty'
        ORDER BY s.product_url
        LIMIT 5
        """
    )
    for row in cur.fetchall():
        print(f"    {row[0]} | price={row[1]} | price_flag={row[2]} | price_raw={row[3]!r}")

    print()
    print("=" * 70)
    print("Extra cross-checks")
    print("=" * 70)
    cur.execute("SELECT count(*) FROM sku_category")
    print(f"sku_category row count: {cur.fetchone()[0]}")

    cur.execute(
        "SELECT s.product_url, string_agg(c.name, ';' ORDER BY c.name) "
        "FROM sku s JOIN sku_category sc ON sc.sku_id = s.id JOIN category c ON c.id = sc.category_id "
        "GROUP BY s.product_url HAVING count(*) > 1 ORDER BY s.product_url"
    )
    multi = cur.fetchall()
    print(f"SKUs with more than one category ({len(multi)} found, expect 13 -- Home Centre's subset case):")
    for row in multi[:3]:
        print(f"    {row[0]} -> {row[1]}")
    print("    ..." if len(multi) > 3 else "")

    cur.execute("SELECT count(*) FROM source_record")
    print(f"source_record row count: {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM price_history")
    print(f"price_history row count: {cur.fetchone()[0]}")
    cur.execute("SELECT count(DISTINCT source_type) FROM source_record")
    print(f"distinct source_type values used: {cur.fetchone()[0]}")

    conn.close()


if __name__ == "__main__":
    main()
