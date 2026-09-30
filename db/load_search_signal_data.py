"""
Loads review_extraction/search_signal_data.csv (search_signal_collector.py's
output) into the PRODUCTION database only. Fixture is never touched by this
script -- this is genuinely real, externally verifiable search-demand data
(Google Trends + Google autocomplete), the same category as
paid_ad_creative's real ad-library rows, not synthetic test data with no
reason to be treated as such.

Source-record sharing: every row in one CSV run shares the exact same
collected_at timestamp (search_signal_collector.py captures it once at the
top of main() and stamps every row with it, regardless of which Trends
batch or autocomplete call produced it) -- so this loader creates ONE
source_record per CSV file/run and FKs every search_signal_data row to it,
rather than one source_record per row. This is the same dedup principle
load_production.py's docstring describes for SKU/PriceHistory pairs: a
second, near-identical source_record for the same collection event would be
redundant, not more correct. If a future CSV somehow contains more than one
distinct collected_at value (e.g. two runs' output concatenated by hand),
this script deliberately creates one source_record PER DISTINCT
collected_at value rather than assuming a single one -- checked, not
assumed.

Pre-flight, BEFORE any insert: confirms 'search_signal' is actually present
in production's real source_type_enum (queried live from pg_enum, not
hardcoded/assumed from setup_schema.py's Python list) -- aborts with no rows
written if not, same discipline as load_production.py.

The whole load runs in one transaction -- committed only if every row loads
cleanly, rolled back entirely on any failure. Usage:
    python db/load_search_signal_data.py
"""

import csv
import os
import sys

import psycopg2

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")
CSV_FILE = os.path.join(os.path.dirname(__file__), "..", "review_extraction", "search_signal_data.csv")
SOURCE_TYPE = "search_signal"


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


def _int_or_none(s):
    return int(s) if s not in (None, "") else None


def _bool_or_none(s):
    if s in (None, ""):
        return None
    return s == "True"


def _str_or_none(s):
    return s if s not in (None, "") else None


def main():
    env = _load_env()
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    print(f"Loaded {len(rows)} rows from {CSV_FILE}")
    if not rows:
        print("Nothing to load.")
        return

    csv_source_types = {r["source_type"] for r in rows}
    if csv_source_types != {SOURCE_TYPE}:
        print(f"ABORTING -- expected every row's source_type to be {SOURCE_TYPE!r}, "
              f"found: {sorted(csv_source_types)}", file=sys.stderr)
        sys.exit(1)

    conn = _connect(env, "PRODUCTION")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # ---- Pre-flight: 'search_signal' must already be a valid
            # production enum label. Abort before writing anything if not.
            cur.execute(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON e.enumtypid = t.oid "
                "WHERE t.typname = 'source_type_enum'"
            )
            valid_source_types = {r[0] for r in cur.fetchall()}
            if SOURCE_TYPE not in valid_source_types:
                print(f"ABORTING -- {SOURCE_TYPE!r} is not a valid production source_type_enum label, "
                      f"refusing to force or default it.", file=sys.stderr)
                print(f"Valid production source_type values: {sorted(valid_source_types)}", file=sys.stderr)
                conn.rollback()
                sys.exit(1)
            print(f"Pre-flight OK -- {SOURCE_TYPE!r} is a valid production source_type_enum label.")

            # ---- one source_record per distinct collected_at value in the
            # CSV (see module docstring for why this isn't one-per-row).
            distinct_timestamps = sorted(set(r["collected_at"] for r in rows))
            source_record_ids = {}
            for ts in distinct_timestamps:
                rows_at_ts = [r for r in rows if r["collected_at"] == ts]
                signal_counts = {}
                for r in rows_at_ts:
                    signal_counts[r["signal_type"]] = signal_counts.get(r["signal_type"], 0) + 1
                note = (f"search_signal_collector.py run: {len(rows_at_ts)} row(s) "
                        f"({', '.join(f'{k}={v}' for k, v in sorted(signal_counts.items()))}); "
                        f"Google Trends (geo=IN) + Google autocomplete")
                cur.execute(
                    "INSERT INTO source_record (source_type, source_url, collected_at, notes) "
                    "VALUES (%s, NULL, %s, %s) RETURNING id",
                    (SOURCE_TYPE, ts, note)
                )
                source_record_ids[ts] = cur.fetchone()[0]
            print(f"Created {len(source_record_ids)} source_record row(s) for "
                  f"{len(distinct_timestamps)} distinct collected_at value(s).")

            # ---- search_signal_data rows
            inserted = 0
            for r in rows:
                cur.execute(
                    """
                    INSERT INTO search_signal_data (
                        signal_type, term, category, term_type, batch_id,
                        date, interest_value, is_partial,
                        query_type, related_query, related_value_raw, related_value_numeric,
                        suggestion_rank, suggestion_text, source_record_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        r["signal_type"], r["term"], _str_or_none(r["category"]), _str_or_none(r["term_type"]),
                        _int_or_none(r["batch_id"]),
                        _str_or_none(r["date"]), _int_or_none(r["interest_value"]), _bool_or_none(r["is_partial"]),
                        _str_or_none(r["query_type"]), _str_or_none(r["related_query"]),
                        _str_or_none(r["related_value_raw"]), _int_or_none(r["related_value_numeric"]),
                        _int_or_none(r["suggestion_rank"]), _str_or_none(r["suggestion_text"]),
                        source_record_ids[r["collected_at"]],
                    )
                )
                inserted += 1

            print(f"Inserted {inserted} search_signal_data rows.")

        conn.commit()
        print("COMMITTED.")
    except Exception:
        conn.rollback()
        print("ROLLED BACK due to error above -- no partial data was left in production.", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
