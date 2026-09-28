"""
Applies db/migrations/003_add_demo_gap_tables.sql to BOTH production and
fixture (schema parity, same convention as apply_migration_002.py). The
DATA loaded into these tables afterward is fixture-only.

Usage:
    python db/apply_migration_003.py
"""
import os

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
MIGRATION_FILE = os.path.join(os.path.dirname(__file__), "migrations", "003_add_demo_gap_tables.sql")

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


def apply(env, prefix):
    print(f"\n--- {prefix} ---")
    conn = _connect(env, prefix)
    conn.autocommit = False
    try:
        with open(MIGRATION_FILE, encoding="utf-8") as f:
            ddl = f.read()
        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        print("  migration applied and committed.")

        with conn.cursor() as cur:
            for t in NEW_TABLES:
                cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = %s", (t,))
                exists = cur.fetchone()[0] > 0
                row_count = None
                if exists:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    row_count = cur.fetchone()[0]
                print(f"  {t}: exists={exists} row_count={row_count}")
    finally:
        conn.close()


def main():
    env = _load_env()
    apply(env, "PRODUCTION")
    apply(env, "FIXTURE")


if __name__ == "__main__":
    main()
