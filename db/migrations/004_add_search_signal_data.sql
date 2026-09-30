-- Migration 004: search_signal_data table. No enum addition needed here --
-- 'search_signal' was already added to PRODUCTION_SOURCE_TYPES in
-- setup_schema.py before this migration was written (confirmed live against
-- both databases' pg_enum before assuming it, per instructions -- it was
-- already there in both).
--
-- Applied identically to BOTH production and fixture (schema parity, same
-- convention as migrations 001-003) -- only the DATA load afterward is
-- production-only (search_signal_collector.py's output is genuinely real,
-- externally verifiable search-demand data, same category as
-- paid_ad_creative's real ad-library rows, not synthetic test data).
--
-- ONE table, not three, even though three genuinely different kinds of row
-- come out of the collector (interest-over-time points, related/rising
-- queries, and autocomplete suggestions). Migration 001's rationale for
-- giving paid_ad_creative its own table was that an ad and a SKU are
-- unrelated entities -- forcing one into the other's columns would produce
-- a pile of always-NULL columns for no honest reason. That reasoning does
-- NOT apply here: all three row kinds describe the exact same entity (a
-- demand signal for one search term, on one date, from one source), just
-- captured three different ways. A `signal_type` discriminator column plus
-- type-specific nullable columns is the same pattern price_history already
-- uses (price nullable, price_flag explains why) rather than a symptom of
-- cramming unrelated data together.
--
-- interest_value is Google Trends' own 0-100 relative-within-batch index --
-- NOT a comparable absolute number across different batch_id values (Trends
-- itself doesn't expose one; see search_signal_collector.py's module
-- docstring). batch_id is mandatory whenever signal_type='interest_over_time'
-- so a later reader can never accidentally compare interest_value across
-- two unrelated batches.
--
-- related_value_raw/related_value_numeric follow price_history's raw+flag
-- precedent: Google Trends' "rising queries" can report either a numeric
-- growth percentage OR the literal string 'Breakout' -- related_value_raw
-- preserves whatever Trends actually returned, related_value_numeric is
-- filled only when it parsed as an integer, never coerced or defaulted.

BEGIN;

CREATE TABLE IF NOT EXISTS search_signal_data (
    id                      BIGSERIAL PRIMARY KEY,
    signal_type             TEXT NOT NULL CHECK (signal_type IN ('interest_over_time', 'related_query', 'autocomplete')),
    term                    TEXT NOT NULL,
    category                TEXT,
    term_type               TEXT CHECK (term_type IN ('brand', 'generic')),
    batch_id                INTEGER,
    date                    DATE,
    interest_value          INTEGER,
    is_partial              BOOLEAN,
    query_type              TEXT CHECK (query_type IN ('top', 'rising')),
    related_query           TEXT,
    related_value_raw       TEXT,
    related_value_numeric   INTEGER,
    suggestion_rank         INTEGER,
    suggestion_text         TEXT,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT search_signal_data_batch_id_required_for_interest CHECK (
        signal_type != 'interest_over_time' OR batch_id IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_search_signal_data_term ON search_signal_data(term);
CREATE INDEX IF NOT EXISTS idx_search_signal_data_signal_type ON search_signal_data(signal_type);
CREATE INDEX IF NOT EXISTS idx_search_signal_data_category ON search_signal_data(category);

COMMIT;
