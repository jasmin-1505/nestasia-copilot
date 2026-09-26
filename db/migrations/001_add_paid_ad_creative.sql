-- Migration 001: paid_ad_creative table + a new source_type enum value.
--
-- Applied identically to BOTH production and fixture (schema parity, same
-- convention as db/schema.sql) -- only the data load afterward is
-- production-only. Kept as its own file rather than editing schema.sql in
-- place, per instructions, so schema history stays legible.
--
-- Ad creative (Meta Ad Library screenshots/metadata) is a genuinely
-- different entity from a SKU or a PriceHistory snapshot -- it has no
-- product_url, no price, and describes a marketing artifact, not a
-- catalogue item. Forcing it into the sku/price_history tables would have
-- meant a pile of always-NULL SKU columns and a stock_mismatch value that
-- means nothing for an ad. A new table matches what the data actually is.
--
-- ---------------------------------------------------------------------
-- Enum addition: 'ad_library_public' is added to BOTH databases' enums,
-- unlike 'synthetic' (fixture-only) -- this is genuinely real, publicly
-- visible data (a live ad on Meta's own public Ad Library), not synthetic
-- test data, regardless of which database it eventually lives in.
--
-- Safety note (verified, not assumed): `ALTER TYPE ... ADD VALUE` adds a
-- new row to the pg_enum catalog and does not rewrite the enum type's
-- underlying storage or any table that uses it -- Postgres enums are
-- stored as 4-byte OIDs referencing pg_enum, so adding a label is a
-- metadata-only change with no table lock beyond a brief catalog update.
-- No downtime, no rewrite, on either database. The one real constraint
-- (also verified against this project's actual Postgres 17.6, which does
-- allow ADD VALUE inside a transaction) is that a newly-added enum value
-- cannot be USED in an INSERT within the same transaction that added it --
-- so this ALTER TYPE statement is committed on its own, before
-- load_paid_ad_creative.py's later INSERTs that reference it.
-- ---------------------------------------------------------------------
ALTER TYPE source_type_enum ADD VALUE IF NOT EXISTS 'ad_library_public';

-- ---------------------------------------------------------------------
-- paid_ad_creative: one row per observed ad (Meta Ad Library entry).
-- Same exactly-one-owner pattern as sku's brand_id/competitor_brand_id
-- CHECK constraint -- an ad belongs to Nestasia's own brand or to a
-- tracked competitor, never both, never neither.
--
-- ad_library_url is NOT NULL: every real row in the source spreadsheet has
-- one, and it is this table's natural identifying/dedup key (no separate
-- product_url concept applies to an ad the way it does to a SKU).
--
-- status is left as free TEXT, not an enum, since Meta's Ad Library status
-- values aren't a fixed set this project controls (all 16 real rows are
-- currently 'Active', but a future load could see others).
--
-- source_record_id is mandatory, same as every other fact table in this
-- schema -- provenance is never optional here either.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS paid_ad_creative (
    id                      BIGSERIAL PRIMARY KEY,
    brand_id                BIGINT REFERENCES brand(id),
    competitor_brand_id     BIGINT REFERENCES competitor_brand(id),
    platform                TEXT,
    ad_library_url          TEXT NOT NULL,
    start_date              DATE,
    status                  TEXT,
    end_date                DATE,
    days_running            INTEGER,
    ad_format               TEXT,
    product_subcategory     TEXT,
    hook_headline           TEXT,
    offer_discount          TEXT,
    cta_button              TEXT,
    landing_page_url        TEXT,
    notes                   TEXT,
    theme                   TEXT,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT paid_ad_creative_exactly_one_owner CHECK (
        (brand_id IS NOT NULL)::int + (competitor_brand_id IS NOT NULL)::int = 1
    )
);

CREATE INDEX IF NOT EXISTS idx_paid_ad_creative_brand ON paid_ad_creative(brand_id);
CREATE INDEX IF NOT EXISTS idx_paid_ad_creative_competitor_brand ON paid_ad_creative(competitor_brand_id);
