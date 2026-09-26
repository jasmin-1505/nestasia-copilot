"""
Applies db/migrations/001_add_paid_ad_creative.sql to BOTH production and
fixture (schema parity, matching db/setup_schema.py's convention) --
'ad_library_public' is genuinely real public data, so unlike 'synthetic'
it belongs in both databases' enums, not just fixture's.

Usage:
    python db/apply_migration.py
"""
import os
import sys

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
MIGRATION_FILE = os.path.join(os.path.dirname(__file__), "migrations", "001_add_paid_ad_creative.sql")


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
            cur.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'source_type_enum' ORDER BY e.enumsortorder"
            )
            print(f"  source_type_enum now: {[r[0] for r in cur.fetchall()]}")

            cur.execute(
                "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name = 'paid_ad_creative' ORDER BY ordinal_position"
            )
            print("  paid_ad_creative columns:")
            for row in cur.fetchall():
                print(f"    {row[0]:<24} {row[1]:<20} nullable={row[2]}")
    finally:
        conn.close()


def main():
    env = _load_env()
    apply(env, "PRODUCTION")
    apply(env, "FIXTURE")


if __name__ == "__main__":
    main()
