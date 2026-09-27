-- Migration 002: synthetic business-data tables for the demo layer.
--
-- Applied to BOTH databases (schema parity, same convention as migration
-- 001) -- but DATA only ever goes into fixture. These tables model the
-- business data Nestasia doesn't share with this project yet (sales,
-- inventory, channel performance, margin, social engagement), so the demo
-- can show the full information flow now and swap in real rows later
-- without a schema change. Production stays structurally ready but
-- genuinely empty until real data exists -- there is no code path in this
-- project that writes real rows to these tables yet.
--
-- Every row carries source_record_id (source_type='synthetic' for
-- fixture's rows -- already a fixture-only enum value, see migration 001)
-- AND a table-level is_synthetic boolean, redundantly. Two independent
-- signals, not one, because this data must be structurally impossible to
-- mistake for real business data even by a query that only checks one of
-- them.

BEGIN;

-- ---------------------------------------------------------------------
-- sales_data: units_sold/revenue per SKU per monthly period.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sales_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    period_start            DATE NOT NULL,
    period_end              DATE NOT NULL,
    units_sold              INTEGER NOT NULL,
    revenue                 NUMERIC(12, 2) NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_sales_data_sku ON sales_data(sku_id);

-- ---------------------------------------------------------------------
-- inventory_data: current stock snapshot per SKU.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inventory_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    as_of_date              DATE NOT NULL,
    current_stock           INTEGER NOT NULL,
    reorder_point           INTEGER NOT NULL,
    days_of_stock_remaining NUMERIC(6, 1),
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inventory_data_sku ON inventory_data(sku_id);

-- ---------------------------------------------------------------------
-- channel_performance: revenue/orders/return_rate per SKU per channel.
-- channel is free TEXT, not an enum -- the six named channels (own
-- website, Amazon, Flipkart, Blinkit, Zepto, Instamart) aren't a schema-
-- level constraint this project controls, and a real integration could add
-- more.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS channel_performance (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    channel                 TEXT NOT NULL,
    revenue                 NUMERIC(12, 2) NOT NULL,
    orders                  INTEGER NOT NULL,
    return_rate             NUMERIC(5, 2) NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_channel_performance_sku ON channel_performance(sku_id);

-- ---------------------------------------------------------------------
-- margin_data: unit_cost/margin_percent per SKU.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS margin_data (
    id                      BIGSERIAL PRIMARY KEY,
    sku_id                  BIGINT NOT NULL REFERENCES sku(id),
    as_of_date              DATE NOT NULL,
    unit_cost               NUMERIC(12, 2) NOT NULL,
    margin_percent          NUMERIC(5, 2) NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_margin_data_sku ON margin_data(sku_id);

-- ---------------------------------------------------------------------
-- social_engagement: likes/comments/shares per post, linked to a brand --
-- same exactly-one-owner pattern as sku/paid_ad_creative. This is
-- brand-level, not SKU-level (a social post isn't about one product), so
-- it can exist for a brand with no catalogue SKUs at all (e.g. IKEA India,
-- which this project only has ad-creative data for).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS social_engagement (
    id                      BIGSERIAL PRIMARY KEY,
    brand_id                BIGINT REFERENCES brand(id),
    competitor_brand_id     BIGINT REFERENCES competitor_brand(id),
    post_date               DATE NOT NULL,
    platform                TEXT,
    likes                   INTEGER NOT NULL,
    comments                INTEGER NOT NULL,
    shares                  INTEGER NOT NULL,
    is_synthetic            BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT social_engagement_exactly_one_owner CHECK (
        (brand_id IS NOT NULL)::int + (competitor_brand_id IS NOT NULL)::int = 1
    )
);
CREATE INDEX IF NOT EXISTS idx_social_engagement_brand ON social_engagement(brand_id);
CREATE INDEX IF NOT EXISTS idx_social_engagement_competitor_brand ON social_engagement(competitor_brand_id);

COMMIT;
