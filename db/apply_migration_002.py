"""
Applies db/migrations/002_add_synthetic_business_tables.sql to BOTH
production and fixture (schema parity, same convention as
db/apply_migration.py for migration 001) -- the DATA loaded into these
tables afterward is fixture-only; this script only creates the (empty)
tables in both.

Usage:
    python db/apply_migration_002.py
"""
import os

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
MIGRATION_FILE = os.path.join(os.path.dirname(__file__), "migrations", "002_add_synthetic_business_tables.sql")

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
                cur.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name = %s", (t,)
                )
                exists = cur.fetchone()[0] > 0
                cur.execute(f"SELECT count(*) FROM {t}") if exists else None
                row_count = cur.fetchone()[0] if exists else None
                print(f"  {t}: exists={exists} row_count={row_count}")
    finally:
        conn.close()


def main():
    env = _load_env()
    apply(env, "PRODUCTION")
    apply(env, "FIXTURE")


if __name__ == "__main__":
    main()
