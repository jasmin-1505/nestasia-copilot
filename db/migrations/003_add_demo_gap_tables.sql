-- Migration 003: five more synthetic demo tables, closing specific gaps
-- found while building the 25-question business demo (Q5, Q10, Q12, Q14,
-- Q16/Q17/Q21). Same convention as migration 002: applied to BOTH
-- databases for schema parity, DATA only ever loaded into fixture, every
-- row carries source_record_id (source_type='synthetic') AND a
-- table-level is_synthetic boolean, redundantly.
--
-- These are NEW, SEPARATE tables, not new columns bolted onto real tables
-- (sku, paid_ad_creative) -- real rows stay purely real; the synthetic
-- layer is always a separate table keyed to them by FK.

BEGIN;

-- ---------------------------------------------------------------------
-- sku_launch_data: fabricated launch date per SKU (no real launch-date
-- field exists anywhere in this schema -- see Q5's gap in the feasibility
-- pass). One row per (non-excluded) SKU.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sku_launch_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    launch_date             DATE NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sku_launch_data_sku ON sku_launch_data(sku_id);

-- ---------------------------------------------------------------------
-- traffic_data: sessions per SKU per channel per period, so conversion
-- (orders / sessions) becomes computable against channel_performance.
-- orders (Q10's gap -- there was no denominator anywhere before this).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS traffic_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    channel                 TEXT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    sessions                INTEGER NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_traffic_data_sku ON traffic_data(sku_id);

-- ---------------------------------------------------------------------
-- channel_inventory: per-channel stock split (inventory_data from
-- migration 002 is per-SKU only, with no channel breakdown -- Q12's gap).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_inventory (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    channel                 TEXT NOT NULL,
    as_of_date              DATE NOT NULL,
    stock                   INTEGER NOT NULL,
    days_of_stock           NUMERIC(6, 1),
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_inventory_sku ON channel_inventory(sku_id);

-- ---------------------------------------------------------------------
-- complaint_data: complaint volume/reason per SKU per channel per period,
-- distinct from channel_performance.return_rate (Q14's gap -- a customer
-- can complain without returning, or return without complaining).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS complaint_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    channel                 TEXT NOT NULL,
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    complaint_count         INTEGER NOT NULL,
    top_reason              TEXT NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_complaint_data_sku ON complaint_data(sku_id);

-- ---------------------------------------------------------------------
-- ad_spend_data: spend/impressions/clicks/attributed_orders per ad,
-- FABRICATING a per-SKU link that does not exist in reality --
-- paid_ad_creative has no sku_id at all (ads are brand-level only in the
-- real ad library). sku_id here is a synthetic ASSIGNMENT for demo
-- purposes (an ad's product_subcategory text matched to a real SKU in
-- that category), not a recovered real fact -- see the loader's notes and
-- the "attribution is fabricated by construction" flag this produces on
-- every Q16/Q17/Q21 answer that uses it.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ad_spend_data (
    id                      BIGSERIAL PRIMARY KEY,
    paid_ad_creative_id     BIGINT NOT NULL REFERENCES paid_ad_creative(id),
    sku_id                  BIGINT REFERENCES sku(id),
    spend                   NUMERIC(12, 2) NOT NULL,
    impressions             INTEGER NOT NULL,
    clicks                  INTEGER NOT NULL,
    attributed_orders       INTEGER NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ad_spend_data_ad ON ad_spend_data(paid_ad_creative_id);
CREATE INDEX IF NOT EXISTS idx_ad_spend_data_sku ON ad_spend_data(sku_id);

COMMIT;
