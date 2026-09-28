"""
Independent verification of the demo-gap-table load (migration 003) --
fresh script, fresh connections, no reliance on load_demo_gap_data.py's
own printed counts.
"""
import os

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
NEW_TABLES = ["sku_launch_data", "traffic_data", "channel_inventory", "complaint_data", "ad_spend_data"]


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

    print("1. PRODUCTION -- must be exactly 0 for all 5 new tables")
    prod = _connect(env, "PRODUCTION")
    cur = prod.cursor()
    for t in NEW_TABLES:
        cur.execute(f"SELECT count(*) FROM {t}")
        print(f"  {t}: {cur.fetchone()[0]}")
    prod.close()

    print("\n2. FIXTURE -- row counts + is_synthetic/source_type double-check")
    fix = _connect(env, "FIXTURE")
    cur = fix.cursor()
    for t in NEW_TABLES:
        cur.execute(
            f"""
            SELECT count(*), count(*) FILTER (WHERE t.is_synthetic = true),
                   count(*) FILTER (WHERE sr.source_type = 'synthetic')
            FROM {t} t JOIN source_record sr ON sr.id = t.source_record_id
            """
        )
        total, is_synth, src_synth = cur.fetchone()
        print(f"  {t}: total={total} is_synthetic=true:{is_synth} source_type=synthetic:{src_synth}")

    print("\n3. Excluded-10-SKU check -- none of the 10 unpriced SKUs got a row anywhere")
    cur.execute(
        """
        SELECT s.id FROM sku s
        WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.sku_id = s.id AND ph.price IS NOT NULL)
        """
    )
    unpriced_ids = [r[0] for r in cur.fetchall()]
    print(f"  {len(unpriced_ids)} unpriced SKUs found")
    for t in ["sku_launch_data", "traffic_data", "channel_inventory", "complaint_data"]:
        cur.execute(f"SELECT count(*) FROM {t} WHERE sku_id = ANY(%s)", (unpriced_ids,))
        print(f"  {t} rows touching an unpriced SKU (should be 0): {cur.fetchone()[0]}")
    cur.execute("SELECT count(*) FROM ad_spend_data WHERE sku_id = ANY(%s)", (unpriced_ids,))
    print(f"  ad_spend_data rows touching an unpriced SKU (should be 0): {cur.fetchone()[0]}")

    print("\n4. paid_ad_creative sync check")
    cur.execute("SELECT count(*) FROM paid_ad_creative")
    print(f"  fixture paid_ad_creative rows: {cur.fetchone()[0]}")

    fix.close()


if __name__ == "__main__":
    main()
