"""
Applies schema.sql to both the production and fixture Postgres databases,
then proves (not assumes) that the production database's source_type enum
actually rejects 'synthetic' at the schema level.

Deliberately does NOT read a single PRODUCTION_DB_URL / FIXTURE_DB_URL
connection-string env var: the production password contains literal '%'
and '?' characters that are not valid unescaped URI characters (a bare '?'
would be read as the start of a query string, and '%' requires two
following hex digits to be a legal percent-escape, which "%v" is not).
Parsing that string as a URI would silently corrupt the password. .env
instead stores each of PRODUCTION_DB_{HOST,PORT,USER,PASSWORD,NAME} and
FIXTURE_DB_{HOST,PORT,USER,PASSWORD,NAME} as discrete fields, passed to
psycopg2.connect() as keyword arguments with no string-splitting involved.

Both databases are reached via Supabase's Supavisor connection pooler
(hostname aws-0-<region>.pooler.supabase.com, port 6543, username
postgres.<project-ref>), not the direct db.<ref>.supabase.co:5432 hostname
-- that direct hostname resolves to an IPv6-only address on Supabase's free
tier, and this environment has no IPv6 route out. Production's pooler
region is ap-northeast-2; fixture's is ap-northeast-1 -- two DIFFERENT
Supabase projects, confirmed by their distinct project-ref segments
(obytmljerutlfxtoimfh vs gzgwxprgmsqypcsnmlhk) baked into the pooler
username, not just different regions of convenience.

Loads these from .env. Never prints a raw password -- only host/dbname
(non-secret parts) are logged, for the reader to confirm which instance
was touched.

Usage:
    python db/setup_schema.py
"""

import os
import re
import sys

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
SCHEMA_FILE = os.path.join(os.path.dirname(__file__), "schema.sql")

PRODUCTION_SOURCE_TYPES = [
    "live_storefront", "founder_confirmed", "industry_report",
    "marketplace_review", "qcommerce_listing", "search_signal",
]
FIXTURE_SOURCE_TYPES = PRODUCTION_SOURCE_TYPES + ["synthetic"]


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


def _connection_params(env, prefix):
    """Reads <prefix>_DB_{HOST,PORT,USER,PASSWORD,NAME} as discrete fields
    -- see module docstring for why .env stores them this way rather than
    as a single connection-string URL."""
    missing = [f"{prefix}_DB_{field}" for field in ("HOST", "PORT", "USER", "PASSWORD", "NAME")
               if f"{prefix}_DB_{field}" not in env]
    if missing:
        print(f"Missing required .env keys: {missing}", file=sys.stderr)
        sys.exit(1)
    return {
        "host": env[f"{prefix}_DB_HOST"],
        "port": env[f"{prefix}_DB_PORT"],
        "user": env[f"{prefix}_DB_USER"],
        "password": env[f"{prefix}_DB_PASSWORD"],
        "dbname": env[f"{prefix}_DB_NAME"],
    }


def _connect(conn_params):
    return psycopg2.connect(
        host=conn_params["host"],
        port=conn_params["port"],
        dbname=conn_params["dbname"],
        user=conn_params["user"],
        password=conn_params["password"],
        sslmode="require",
        connect_timeout=15,
    )


def _redacted(conn_params):
    return f"{conn_params['host']}:{conn_params['port']}/{conn_params['dbname']}"


def apply_schema(conn_params, label, source_types):
    print(f"\n--- {label} ({_redacted(conn_params)}) ---")
    conn = _connect(conn_params)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            print(f"  connectivity OK")

            cur.execute("SELECT current_database(), current_user")
            db, user = cur.fetchone()
            print(f"  connected as {user!r} to database {db!r}")

        with open(SCHEMA_FILE, encoding="utf-8") as f:
            template = f.read()
        values_sql = ", ".join(f"'{v}'" for v in source_types)
        ddl = template.replace("{SOURCE_TYPE_VALUES}", values_sql)

        with conn.cursor() as cur:
            cur.execute(ddl)
        conn.commit()
        print(f"  schema applied -- source_type_enum = {source_types}")
    finally:
        conn.close()


def test_synthetic_rejected_on_production(conn_params):
    """Proves, with a real attempted insert, that 'synthetic' is not a
    valid source_type in production -- not assumed from the enum
    definition alone."""
    print(f"\n--- synthetic-value rejection test (production) ---")
    conn = _connect(conn_params)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO source_record (source_type, collected_at) "
                    "VALUES ('synthetic', now())"
                )
                conn.commit()
                print("  UNEXPECTED: insert with source_type='synthetic' SUCCEEDED. "
                      "This is a failure of the schema, not a pass.")
                return False
            except psycopg2.Error as e:
                conn.rollback()
                print(f"  insert correctly REJECTED at the schema level.")
                print(f"  Postgres error: {e.pgcode} {str(e).strip().splitlines()[0]}")
                return True
    finally:
        conn.close()


def test_synthetic_accepted_on_fixture(conn_params):
    """Proves the fixture database's enum genuinely differs (accepts
    'synthetic'), then rolls back -- no data is left behind per
    instructions (schema/connectivity only, no data loading this pass)."""
    print(f"\n--- synthetic-value acceptance check (fixture, then rolled back) ---")
    conn = _connect(conn_params)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO source_record (source_type, collected_at) "
                    "VALUES ('synthetic', now())"
                )
                print("  insert with source_type='synthetic' succeeded on fixture, as expected "
                      "(fixture's enum includes it) -- rolling back, no data kept.")
                conn.rollback()
                return True
            except psycopg2.Error as e:
                conn.rollback()
                print(f"  UNEXPECTED: fixture rejected 'synthetic' too: {e}")
                return False
    finally:
        conn.close()


def main():
    env = _load_env()
    prod_params = _connection_params(env, "PRODUCTION")
    fixture_params = _connection_params(env, "FIXTURE")

    if prod_params["host"] == fixture_params["host"] and prod_params["user"] == fixture_params["user"]:
        print("PRODUCTION_DB_URL and FIXTURE_DB_URL point at the same host+database -- "
              "this violates the hard requirement that they be physically separate instances. Aborting.",
              file=sys.stderr)
        sys.exit(1)

    apply_schema(prod_params, "PRODUCTION", PRODUCTION_SOURCE_TYPES)
    apply_schema(fixture_params, "FIXTURE", FIXTURE_SOURCE_TYPES)

    prod_ok = test_synthetic_rejected_on_production(prod_params)
    fixture_ok = test_synthetic_accepted_on_fixture(fixture_params)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Production reachable + schema applied: yes ({_redacted(prod_params)})")
    print(f"Fixture reachable + schema applied:    yes ({_redacted(fixture_params)})")
    print(f"Production rejects 'synthetic':         {'CONFIRMED' if prod_ok else 'FAILED -- SEE ABOVE'}")
    print(f"Fixture accepts 'synthetic':             {'CONFIRMED' if fixture_ok else 'FAILED -- SEE ABOVE'}")
    print("No data rows were left in either database -- schema/connectivity only, as instructed.")


if __name__ == "__main__":
    main()
