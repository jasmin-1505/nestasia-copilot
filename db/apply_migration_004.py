"""
Applies db/migrations/004_add_search_signal_data.sql to BOTH production and
fixture (schema parity, same convention as apply_migration_002.py/_003.py).
The DATA loaded into this table afterward is production-only (see
db/load_search_signal_data.py) -- search_signal_collector.py's output is
genuinely real, externally verifiable data, not synthetic.

Usage:
    python db/apply_migration_004.py
"""
import os

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
MIGRATION_FILE = os.path.join(os.path.dirname(__file__), "migrations", "004_add_search_signal_data.sql")

NEW_TABLES = ["search_signal_data"]


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

            cur.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'source_type_enum' ORDER BY e.enumsortorder"
            )
            print(f"  source_type_enum: {[r[0] for r in cur.fetchall()]}")
    finally:
        conn.close()


def main():
    env = _load_env()
    apply(env, "PRODUCTION")
    apply(env, "FIXTURE")


if __name__ == "__main__":
    main()
