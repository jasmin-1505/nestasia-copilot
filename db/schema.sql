-- Nestasia competitive-intelligence schema.
--
-- Applied identically to BOTH the production and fixture databases, with
-- exactly one deliberate difference: the source_type enum. Production's
-- excludes 'synthetic' entirely (it is not a valid label in that database's
-- pg_enum catalog, not just "discouraged by convention"); the fixture
-- database's includes it. {SOURCE_TYPE_VALUES} is substituted by
-- setup_schema.py per-target before this file is executed.
--
-- Column choices trace directly to what normalize.py's real 954-row output
-- produces and to the collection sessions that produced it -- see this
-- repo's review_extraction/NOTES.md for the underlying findings.

BEGIN;

CREATE TYPE source_type_enum AS ENUM ({SOURCE_TYPE_VALUES});

-- stock_mismatch is not a boolean: normalize.py's bucketing (see
-- review_extraction/normalize.py) produces exactly four values, and
-- collapsing "N/A" or "Unknown" into false would misrepresent an
-- inapplicable or ambiguous case as a confirmed-clean one -- the mistake
-- this whole project's collection sessions were built to avoid. Enum
-- labels match normalize.py's own string vocabulary exactly, so loading
-- its CSV output later needs no translation step.
CREATE TYPE stock_mismatch_enum AS ENUM ('true', 'false', 'N/A', 'Unknown');

-- price_flag: 'parsed' and 'empty' are what normalize.py actually emits
-- today (10 real Prestige Out-of-Stock rows have price_flag='empty' with
-- no numeric price at all -- a genuine site-side gap, not something to
-- paper over with a guessed value). 'unparseable' and 'estimated' are
-- included because normalize.py's own parser already distinguishes
-- "unparseable" as a distinct failure mode, and 'estimated' is reserved for
-- a not-yet-built future case (e.g. a manually-entered fallback price),
-- named now so it doesn't require an enum migration later.
CREATE TYPE price_flag_enum AS ENUM ('parsed', 'empty', 'unparseable', 'estimated');

-- ---------------------------------------------------------------------
-- SourceRecord: mandatory FK target for every SKU / PriceHistory /
-- ReviewSet row -- provenance is never optional in this schema.
-- ---------------------------------------------------------------------
CREATE TABLE source_record (
    id              BIGSERIAL PRIMARY KEY,
    source_type     source_type_enum NOT NULL,
    source_url      TEXT,
    collected_at    TIMESTAMPTZ NOT NULL,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Brand: Nestasia's own side of the data (own_site_collector.py's output).
-- Kept separate from CompetitorBrand per the brief -- the two sides of
-- this dataset have different collection tooling and different platform
-- assumptions, and conflating them was never how this project's own
-- scripts modeled it either (nestasia_catalogue.csv vs
-- competitor_catalogue.csv are and always were separate files).
-- ---------------------------------------------------------------------
CREATE TABLE brand (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- CompetitorBrand: Wonderchef, Home Centre, Milton, Prestige, Borosil
-- (Borosil not yet built -- see review_extraction/NOTES.md; row can exist
-- here with platform=NULL ahead of its own collector being written).
-- platform matches competitor_collector.py's PLATFORM_COLLECTORS keys
-- exactly (shopify_t4s, unbxd_nextjs, shopify_hyper_sections,
-- magento_luma) so this table can be cross-referenced against that code
-- without a translation table.
-- ---------------------------------------------------------------------
CREATE TABLE competitor_brand (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    platform        TEXT,
    base_url        TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- Category / Subcategory: Category is the conceptual grouping shared
-- across brand and competitors (Cookware, Bakeware, Container, Lunch
-- Boxes+Bags, Kitchen Racks+Trivets). Subcategory is the finer,
-- collection-URL-level entity beneath it (own_site_collector.py's own
-- nestasia_subcategories.csv already models this: one "Container" label
-- spans two real URLs, jars-canisters and fridge-storage-containers).
-- ---------------------------------------------------------------------
CREATE TABLE category (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subcategory (
    id              BIGSERIAL PRIMARY KEY,
    category_id     BIGINT NOT NULL REFERENCES category(id),
    name            TEXT NOT NULL,
    url             TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (category_id, name)
);

-- ---------------------------------------------------------------------
-- SKU: one row per unique product (per brand OR competitor_brand, never
-- both -- the CHECK below enforces that split explicitly rather than
-- leaving it as an unenforced convention).
--
-- categories: normalize.py's real output found the SAME product_url
-- (Home Centre) legitimately belonging to more than one category
-- (Bakeware is a confirmed subset of Cookware -- see
-- review_extraction/NOTES.md finding (i)). This schema uses a proper
-- many-to-many join table (sku_category, below) rather than a delimited
-- string column -- Postgres makes this no harder to declare, and it
-- avoids reintroducing, at the database layer, exactly the kind of
-- double-counting bug normalize.py's dedup logic had to fix in
-- application code. (The brief permits a documented delimited string as a
-- fallback if time doesn't allow the join table -- noting here explicitly
-- that the join table was used instead.)
--
-- stock_mismatch and collection_complete are NOT NULL by design -- see
-- the type comments above and the brief: a row must never be ambiguous
-- between "not checked" and a real value, and every row must explicitly
-- state whether its source collection run was complete or partial.
-- ---------------------------------------------------------------------
CREATE TABLE sku (
    id                      BIGSERIAL PRIMARY KEY,
    brand_id                BIGINT REFERENCES brand(id),
    competitor_brand_id     BIGINT REFERENCES competitor_brand(id),
    subcategory_id          BIGINT REFERENCES subcategory(id),
    product_name            TEXT NOT NULL,
    product_url             TEXT NOT NULL UNIQUE,
    material_type_tag       TEXT,
    stock_status_tag        TEXT,
    actual_button_state     TEXT,
    stock_mismatch          stock_mismatch_enum NOT NULL,
    collection_complete     BOOLEAN NOT NULL,
    source_record_id        BIGINT NOT NULL REFERENCES source_record(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sku_exactly_one_owner CHECK (
        (brand_id IS NOT NULL)::int + (competitor_brand_id IS NOT NULL)::int = 1
    )
);

CREATE TABLE sku_category (
    sku_id          BIGINT NOT NULL REFERENCES sku(id) ON DELETE CASCADE,
    category_id     BIGINT NOT NULL REFERENCES category(id),
    PRIMARY KEY (sku_id, category_id)
);

-- ---------------------------------------------------------------------
-- PriceHistory: price is nullable ON PURPOSE -- 10 real Prestige
-- Out-of-Stock rows have no price at all because their grid card doesn't
-- render a price box (see review_extraction/NOTES.md finding (m)). That
-- is a genuine, correctly-flagged state (price_flag='empty'), not
-- something to paper over with a guessed value or a coerced zero.
-- ---------------------------------------------------------------------
CREATE TABLE price_history (
    id                  BIGSERIAL PRIMARY KEY,
    sku_id              BIGINT NOT NULL REFERENCES sku(id),
    price               NUMERIC(12, 2),
    price_flag          price_flag_enum NOT NULL,
    price_raw           TEXT,
    compare_at_price    NUMERIC(12, 2),
    discount_percent    NUMERIC(5, 2),
    collected_at        TIMESTAMPTZ NOT NULL,
    source_record_id    BIGINT NOT NULL REFERENCES source_record(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- ReviewSet: schema-only for now -- no review data has been loaded by any
-- script yet (extract_reviews.py's reviews_dataset.csv output). sku_id is
-- nullable because a review may reference a product this project's own
-- SKU collectors haven't captured yet.
-- ---------------------------------------------------------------------
CREATE TABLE review_set (
    id                  BIGSERIAL PRIMARY KEY,
    sku_id              BIGINT REFERENCES sku(id),
    rating              NUMERIC(3, 2),
    review_date         DATE,
    review_text         TEXT,
    reviewer_name       TEXT,
    verified_purchase   BOOLEAN,
    source_record_id    BIGINT NOT NULL REFERENCES source_record(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- ChannelListing: where/how a SKU is listed beyond its home storefront
-- (marketplace, quick-commerce, etc.) -- kept generic since no channel
-- data has been collected yet, only modeled ahead of it.
-- ---------------------------------------------------------------------
CREATE TABLE channel_listing (
    id                  BIGSERIAL PRIMARY KEY,
    sku_id              BIGINT NOT NULL REFERENCES sku(id),
    channel_type        TEXT NOT NULL,
    channel_url         TEXT,
    listing_price       NUMERIC(12, 2),
    in_stock            BOOLEAN,
    source_record_id    BIGINT NOT NULL REFERENCES source_record(id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sku_brand ON sku(brand_id);
CREATE INDEX idx_sku_competitor_brand ON sku(competitor_brand_id);
CREATE INDEX idx_sku_collection_complete ON sku(collection_complete);
CREATE INDEX idx_sku_stock_mismatch ON sku(stock_mismatch);
CREATE INDEX idx_price_history_sku ON price_history(sku_id);
CREATE INDEX idx_review_set_sku ON review_set(sku_id);
CREATE INDEX idx_channel_listing_sku ON channel_listing(sku_id);

COMMIT;
