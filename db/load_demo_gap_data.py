"""
Generates and loads FIXTURE-ONLY synthetic data into the 5 gap-filling
tables from migration 003: sku_launch_data, traffic_data,
channel_inventory, complaint_data, ad_spend_data. Production is never
written to.

Independence from load_synthetic_business_data.py's generation, by
instruction: this script uses its OWN random seed and, wherever avoidable,
does NOT derive numbers from the same real signals (price, discount_
percent, ad-activity) that drove sales_data/margin_data/channel_
performance/social_engagement. Two relationships are NOT avoidable by
construction, and are flagged explicitly (both here and in the
source_record.notes each table's rows link to):

  1. channel_inventory.stock is a per-channel SPLIT of each SKU's existing
     synthetic inventory_data.current_stock (with random channel weights).
     A channel-level stock figure has to relate to the SKU's total stock
     to mean anything at all -- there's no independent "real" total to
     split from instead. days_of_stock further divides by that SKU's
     channel_performance.orders, for the same reason (days-of-stock is
     definitionally stock / velocity).

  2. ad_spend_data.sku_id is a FABRICATED per-ad product assignment.
     paid_ad_creative has no sku_id in reality (ads are brand-level only,
     confirmed during the Step-0 feasibility pass) -- this script invents
     which SKU each ad "targets" by matching the ad's real
     product_subcategory text to a real SKU in that brand+category. This
     is not a recovered fact; it is demo scaffolding, and every answer
     that uses ad_spend_data must say so.

sku_launch_data, traffic_data, and complaint_data use independent noise
with no derivation from price/discount/ad-activity at all.

The same 10 unpriced Prestige SKUs excluded from sales_data/margin_data/
channel_performance are excluded here too (sku_launch_data, traffic_data,
channel_inventory, complaint_data all skip them; ad_spend_data can never
be assigned to them since assignment only draws from priced SKUs).

Usage:
    python db/load_demo_gap_data.py
Requires: db/sync_catalogue_to_fixture.py, db/sync_ads_to_fixture.py, and
db/load_synthetic_business_data.py to have already run (this script reads
fixture's own sku/price_history/inventory_data/channel_performance/
paid_ad_creative rows to build on top of them).
"""
import datetime
import os
import random

import psycopg2
import psycopg2.extras

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

random.seed(20260930)  # different from load_synthetic_business_data.py's seed -- independent noise stream

CHANNELS = ["Own Website", "Amazon", "Flipkart", "Blinkit", "Zepto", "Instamart"]
COMPLAINT_REASONS = [
    "Damaged in transit", "Wrong item received", "Late delivery",
    "Product quality", "Packaging issue", "Size/fit mismatch",
]
N_MONTHS = 6

# Loose text match from paid_ad_creative.product_subcategory to this
# project's real category names.
SUBCATEGORY_TO_CATEGORY = {
    "cookware & utensils": "Cookware",
    "container": "Container",
    "lunch box + bag": "Lunch Boxes+Bags",
}

TABLES = ["sku_launch_data", "traffic_data", "channel_inventory", "complaint_data", "ad_spend_data"]


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


def _month_periods(n, today):
    periods = []
    year, month = today.year, today.month
    for _ in range(n):
        start = datetime.date(year, month, 1)
        end = datetime.date(year, 12, 31) if month == 12 else datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        periods.append((start, end))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(periods))


def main():
    env = _load_env()
    fix = _connect(env, "FIXTURE")
    fix.autocommit = False
    try:
        cur = fix.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        for t in TABLES:
            cur.execute(f"SELECT count(*) AS c FROM {t}")
            if cur.fetchone()["c"] > 0:
                raise SystemExit(f"ABORTING -- fixture.{t} already has rows. Not re-loading.")

        cur.execute(
            """
            SELECT s.id AS sku_id, COALESCE(b.name, cb.name) AS brand_name,
                   ph.price
            FROM sku s
            LEFT JOIN brand b ON b.id = s.brand_id
            LEFT JOIN competitor_brand cb ON cb.id = s.competitor_brand_id
            LEFT JOIN price_history ph ON ph.sku_id = s.id
            """
        )
        raw_rows = cur.fetchall()
        # De-duplicate: a SKU can have >1 price_history row; treat "priced" as
        # having at least one non-NULL price, not one row per price row.
        brand_by_sku, priced_ids_seen = {}, set()
        for r in raw_rows:
            brand_by_sku[r["sku_id"]] = r["brand_name"]
            if r["price"] is not None:
                priced_ids_seen.add(r["sku_id"])
        all_skus = [{"sku_id": sid, "brand_name": brand_by_sku[sid],
                     "price": 1 if sid in priced_ids_seen else None}
                    for sid in brand_by_sku]
        priced_sku_ids = [r["sku_id"] for r in all_skus if r["price"] is not None]
        unpriced_sku_ids = {r["sku_id"] for r in all_skus if r["price"] is None}
        print(f"Pre-flight OK -- {len(priced_sku_ids)} priced SKUs, {len(unpriced_sku_ids)} unpriced "
              f"(excluded from every gap table below, same as the earlier synthetic tables).")

        cur.execute("SELECT sku_id, category_id FROM sku_category")
        sku_category_ids = {}
        for r in cur.fetchall():
            sku_category_ids.setdefault(r["sku_id"], []).append(r["category_id"])
        cur.execute("SELECT id, name FROM category")
        category_name_by_id = {r["id"]: r["name"] for r in cur.fetchall()}
        sku_category_names = {sid: {category_name_by_id[cid] for cid in cids} for sid, cids in sku_category_ids.items()}

        sku_brand_by_id = {r["sku_id"]: r["brand_name"] for r in all_skus}

        cur.execute("SELECT sku_id, current_stock FROM inventory_data")
        current_stock_by_sku = {r["sku_id"]: r["current_stock"] for r in cur.fetchall()}
        cur.execute("SELECT sku_id, channel, orders FROM channel_performance")
        orders_by_sku_channel = {}
        for r in cur.fetchall():
            orders_by_sku_channel[(r["sku_id"], r["channel"])] = r["orders"]

        cur.execute(
            """
            SELECT pac.id AS ad_id, COALESCE(b.name, cb.name) AS brand_name, pac.product_subcategory
            FROM paid_ad_creative pac
            LEFT JOIN brand b ON b.id = pac.brand_id
            LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
            """
        )
        ads = cur.fetchall()

        by_brand_category_skus = {}
        for sid in priced_sku_ids:
            brand = sku_brand_by_id.get(sid)
            for cat in sku_category_names.get(sid, set()):
                by_brand_category_skus.setdefault((brand, cat), []).append(sid)

        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.date()
        periods = _month_periods(N_MONTHS, today)

        def make_source_record(notes):
            cur.execute(
                "INSERT INTO source_record (source_type, source_url, collected_at, notes) "
                "VALUES ('synthetic', NULL, %s, %s) RETURNING id",
                (now, notes),
            )
            return cur.fetchone()["id"]

        sr_launch = make_source_record(
            "Synthetic sku_launch_data for demo purposes. Independent random launch dates -- "
            "not derived from price, discount, or any other real signal. No real launch-date "
            "field exists anywhere in this schema."
        )
        sr_traffic = make_source_record(
            "Synthetic traffic_data for demo purposes. Independent random session counts -- "
            "not derived from price/discount, so conversion = orders/sessions is not circular "
            "with how sales_data was generated."
        )
        sr_channel_inv = make_source_record(
            "Synthetic channel_inventory for demo purposes. UNAVOIDABLE RELATIONSHIP: stock is a "
            "random per-channel split of this SKU's existing synthetic inventory_data.current_stock "
            "(a channel figure has to relate to the SKU's total stock to be coherent at all); "
            "days_of_stock further divides by this SKU's channel_performance.orders for the same "
            "structural reason."
        )
        sr_complaint = make_source_record(
            "Synthetic complaint_data for demo purposes. Fully independent random complaint counts "
            "and reasons -- deliberately NOT derived from channel_performance.return_rate, so "
            "returns and complaints can genuinely disagree in this demo, same as in reality."
        )
        sr_ad_spend = make_source_record(
            "Synthetic ad_spend_data for demo purposes. FABRICATED ATTRIBUTION BY CONSTRUCTION: "
            "paid_ad_creative has no sku_id in reality (ads are brand-level only); sku_id here is "
            "invented by matching each ad's real product_subcategory text to a real SKU in that "
            "brand+category. attributed_orders is independently random, not derived from any real "
            "sales figure, but the underlying sku_id link itself is not a recovered fact."
        )

        # ---- sku_launch_data: independent random launch date, last ~3 years
        launch_rows = []
        for sid in priced_sku_ids:
            days_ago = random.randint(30, 3 * 365)
            launch_rows.append((sid, today - datetime.timedelta(days=days_ago), True, sr_launch))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO sku_launch_data (sku_id, launch_date, is_synthetic, source_record_id) VALUES %s",
            launch_rows,
        )
        print(f"sku_launch_data: inserted {len(launch_rows)} rows")

        # ---- traffic_data: independent random sessions per sku x channel x period
        traffic_rows = []
        for sid in priced_sku_ids:
            base_sessions = random.uniform(20, 300)  # independent per-SKU baseline, unrelated to price
            for ch in CHANNELS:
                ch_weight = random.uniform(0.5, 1.5)
                for start, end in periods:
                    sessions = max(1, round(base_sessions * ch_weight * random.uniform(0.6, 1.4)))
                    traffic_rows.append((sid, ch, start, end, sessions, True, sr_traffic))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO traffic_data (sku_id, channel, period_start, period_end, sessions, is_synthetic, source_record_id) VALUES %s",
            traffic_rows,
        )
        print(f"traffic_data: inserted {len(traffic_rows)} rows")

        # ---- channel_inventory: split of inventory_data.current_stock (flagged unavoidable relationship)
        channel_inv_rows = []
        for sid in priced_sku_ids:
            total_stock = current_stock_by_sku.get(sid)
            if total_stock is None:
                continue
            weights = {ch: random.uniform(0.5, 1.5) for ch in CHANNELS}
            total_w = sum(weights.values())
            for ch, w in weights.items():
                stock = round(total_stock * (w / total_w))
                orders = orders_by_sku_channel.get((sid, ch)) or 0
                days_of_stock = round(stock / (orders / 30), 1) if orders else None
                channel_inv_rows.append((sid, ch, today, stock, days_of_stock, True, sr_channel_inv))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO channel_inventory (sku_id, channel, as_of_date, stock, days_of_stock, is_synthetic, source_record_id) VALUES %s",
            channel_inv_rows,
        )
        print(f"channel_inventory: inserted {len(channel_inv_rows)} rows")

        # ---- complaint_data: fully independent random counts/reasons
        complaint_rows = []
        for sid in priced_sku_ids:
            for ch in CHANNELS:
                for start, end in periods:
                    count = random.choices([0, 1, 2, 3, 4, 5], weights=[40, 25, 15, 10, 6, 4])[0]
                    reason = random.choice(COMPLAINT_REASONS)
                    complaint_rows.append((sid, ch, start, end, count, reason, True, sr_complaint))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO complaint_data (sku_id, channel, period_start, period_end, complaint_count, top_reason, is_synthetic, source_record_id) VALUES %s",
            complaint_rows,
        )
        print(f"complaint_data: inserted {len(complaint_rows)} rows")

        # ---- ad_spend_data: fabricated sku attribution + independent spend/impressions/clicks
        ad_spend_rows = []
        unmatched_ads = 0
        for ad in ads:
            subcat = (ad["product_subcategory"] or "").strip().lower()
            category = SUBCATEGORY_TO_CATEGORY.get(subcat)
            candidates = by_brand_category_skus.get((ad["brand_name"], category), []) if category else []
            sku_id = random.choice(candidates) if candidates else None
            if sku_id is None:
                unmatched_ads += 1
            impressions = random.randint(5000, 200000)
            clicks = max(1, round(impressions * random.uniform(0.005, 0.03)))
            spend = round(clicks * random.uniform(5, 25), 2)
            attributed_orders = max(0, round(clicks * random.uniform(0.01, 0.08)))
            ad_spend_rows.append((ad["ad_id"], sku_id, spend, impressions, clicks, attributed_orders, True, sr_ad_spend))
        psycopg2.extras.execute_values(
            cur, "INSERT INTO ad_spend_data (paid_ad_creative_id, sku_id, spend, impressions, clicks, attributed_orders, is_synthetic, source_record_id) VALUES %s",
            ad_spend_rows,
        )
        print(f"ad_spend_data: inserted {len(ad_spend_rows)} rows ({unmatched_ads} ads had no matching brand+category SKU -- sku_id left NULL for those)")

        fix.commit()
        print("\nCOMMITTED.")
    except Exception:
        fix.rollback()
        print("ROLLED BACK -- no partial data left in fixture.")
        raise
    finally:
        fix.close()


if __name__ == "__main__":
    main()
