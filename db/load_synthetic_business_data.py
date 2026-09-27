"""
Generates and loads synthetic business data into FIXTURE ONLY --
sales_data, inventory_data, channel_performance, margin_data,
social_engagement. Production is never written to by this script; it is
only ever read from (real price/discount/ad data, to inform generation so
the numbers are "loosely realistic," not pure noise).

Deviation from the brief, flagged honestly rather than silently worked
around: the brief asked to correlate synthetic sales with "real review
counts/ratings in production." Checked directly before writing any
generation logic -- production's review_set table has 0 rows (the JTBD
theme-extraction batch has never run; see review_extraction/NOTES.md).
There is no real review/rating data anywhere in this schema to correlate
against. Substituted the two real, per-SKU signals that DO exist instead:
  - price (from price_history) -- cheaper items get a higher baseline
    synthetic sales volume, a standard real-world retail pattern, not an
    arbitrary choice.
  - discount_percent -- a modest boost to synthetic units_sold, since a
    real discount plausibly correlates with real promotional push.
margin_data's unit_cost is also derived from each SKU's real price
(a randomized 35-65% cost-of-goods ratio), and social_engagement's
per-brand "activity level" is derived from each brand's REAL
paid_ad_creative row count and average days_running -- so every one of the
five new tables is grounded in at least one real, already-collected
signal, even without review data.

Every row gets is_synthetic=true (table-level column) AND is linked to a
source_record with source_type='synthetic' (fixture-only enum value) --
two independent, redundant signals, per instructions.

10 SKUs (all Prestige, confirmed Out-of-Stock rows whose grid card never
rendered a price box -- see NOTES.md finding (m)) have no real price at
all. sales_data, margin_data, and channel_performance are SKIPPED for
these 10 specifically, rather than fabricating a price to compute a
synthetic revenue/margin/channel figure from -- inventory_data and
social_engagement don't need a price and are unaffected.

Usage:
    python db/load_synthetic_business_data.py
Requires db/sync_catalogue_to_fixture.py to have already run (fixture must
have the real SKU/brand/price data copied in first).
"""
import datetime
import os
import random

import psycopg2
import psycopg2.extras

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

random.seed(20260928)  # deterministic output -- re-running this script produces the same numbers

CHANNELS = {
    "Own Website": 0.15,
    "Amazon": 0.30,
    "Flipkart": 0.25,
    "Blinkit": 0.10,
    "Zepto": 0.10,
    "Instamart": 0.10,
}
QCOMMERCE_CHANNELS = {"Blinkit", "Zepto", "Instamart"}

N_MONTHS = 6


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
    """Returns n (period_start, period_end) tuples for the n most recent
    whole months, oldest first."""
    periods = []
    year, month = today.year, today.month
    for _ in range(n):
        start = datetime.date(year, month, 1)
        if month == 12:
            end = datetime.date(year, 12, 31)
        else:
            end = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
        periods.append((start, end))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(periods))


def main():
    env = _load_env()
    prod = _connect(env, "PRODUCTION")
    fix = _connect(env, "FIXTURE")
    fix.autocommit = False
    try:
        prod_cur = prod.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        fix_cur = fix.cursor()

        # ---- Pre-flight: refuse to double-load
        for t in ("sales_data", "inventory_data", "channel_performance", "margin_data", "social_engagement"):
            fix_cur.execute(f"SELECT count(*) FROM {t}")
            if fix_cur.fetchone()[0] > 0:
                raise SystemExit(f"ABORTING -- fixture.{t} already has rows. Not re-loading.")

        # ---- Pre-flight: fixture must already have the synced catalogue
        fix_cur.execute("SELECT count(*) FROM sku")
        fixture_sku_count = fix_cur.fetchone()[0]
        if fixture_sku_count == 0:
            raise SystemExit("ABORTING -- fixture.sku is empty. Run db/sync_catalogue_to_fixture.py first.")
        print(f"Pre-flight OK -- fixture has {fixture_sku_count} synced SKUs, all 5 target tables empty.")

        # ---- Read real data from PRODUCTION (read-only)
        prod_cur.execute(
            """
            SELECT s.id AS sku_id, s.product_url, COALESCE(b.name, cb.name) AS brand_name,
                   s.stock_status_tag, ph.price, ph.discount_percent
            FROM sku s
            LEFT JOIN brand b ON b.id = s.brand_id
            LEFT JOIN competitor_brand cb ON cb.id = s.competitor_brand_id
            LEFT JOIN price_history ph ON ph.sku_id = s.id
            """
        )
        prod_skus = prod_cur.fetchall()

        prod_cur.execute(
            """
            SELECT COALESCE(b.name, cb.name) AS brand_name, count(*) AS ad_count, AVG(days_running) AS avg_days_running
            FROM paid_ad_creative pac
            LEFT JOIN brand b ON b.id = pac.brand_id
            LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
            GROUP BY 1
            """
        )
        real_ad_activity = {r["brand_name"]: r for r in prod_cur.fetchall()}
        print(f"Read {len(prod_skus)} real SKU/price rows and {len(real_ad_activity)} brands' "
              f"real ad-activity stats from production (read-only).")

        priced = [r for r in prod_skus if r["price"] is not None]
        unpriced = [r for r in prod_skus if r["price"] is None]
        print(f"  {len(priced)} SKUs have a real price; {len(unpriced)} do not "
              f"(sales_data/margin_data/channel_performance will be skipped for those {len(unpriced)}).")

        min_price = min(r["price"] for r in priced)
        max_price = max(r["price"] for r in priced)
        price_range = float(max_price - min_price) or 1.0

        # ---- Map fixture product_url -> fixture sku_id, and brand_name -> (kind, id)
        fix_cur.execute("SELECT id, product_url FROM sku")
        fixture_sku_id_by_url = {url: sid for sid, url in fix_cur.fetchall()}

        fix_cur.execute("SELECT id, name FROM brand")
        fixture_brand_id = {name: sid for sid, name in fix_cur.fetchall()}
        fix_cur.execute("SELECT id, name FROM competitor_brand")
        fixture_competitor_id = {name: sid for sid, name in fix_cur.fetchall()}

        # ---- One source_record per table, source_type='synthetic'
        now = datetime.datetime.now(datetime.timezone.utc)
        today = now.date()

        def make_source_record(notes):
            fix_cur.execute(
                "INSERT INTO source_record (source_type, source_url, collected_at, notes) "
                "VALUES ('synthetic', NULL, %s, %s) RETURNING id",
                (now, notes),
            )
            return fix_cur.fetchone()[0]

        sr_sales = make_source_record(
            "Synthetic sales_data generated for demo purposes. Correlated with real price "
            "(price_history) and real discount_percent from production, NOT review/rating data -- "
            "production's review_set table has 0 rows as of generation time."
        )
        sr_inventory = make_source_record(
            "Synthetic inventory_data generated for demo purposes. current_stock informed by the "
            "real stock_status_tag from production; reorder_point derived from this same script's "
            "synthetic sales_data for internal consistency."
        )
        sr_channel = make_source_record(
            "Synthetic channel_performance generated for demo purposes. Per-channel revenue split "
            "from a real-price-informed baseline using fixed relative channel weights."
        )
        sr_margin = make_source_record(
            "Synthetic margin_data generated for demo purposes. unit_cost derived from each SKU's "
            "real price (price_history) using a randomized 35-65% cost-of-goods ratio."
        )
        sr_social = make_source_record(
            "Synthetic social_engagement generated for demo purposes. Per-brand activity level "
            "derived from each brand's real paid_ad_creative row count and average days_running "
            "in production."
        )

        periods = _month_periods(N_MONTHS, today)

        # ---- Generate sales_data + track per-SKU avg monthly units/revenue
        # for inventory_data and channel_performance to stay internally
        # consistent with sales_data rather than being independently random.
        sales_rows = []
        per_sku_avg_units = {}
        per_sku_avg_revenue = {}
        skipped_no_fixture_match = 0
        for r in priced:
            fsku = fixture_sku_id_by_url.get(r["product_url"])
            if fsku is None:
                skipped_no_fixture_match += 1
                continue
            price = float(r["price"])
            percentile = (price - float(min_price)) / price_range
            discount = float(r["discount_percent"]) if r["discount_percent"] is not None else 0.0
            base = 5 + (1 - percentile) * 45 + discount * 0.3

            units_this_sku = []
            for i, (start, end) in enumerate(periods):
                trend = 1 + (i - (N_MONTHS - 1) / 2) * 0.03
                noise = random.uniform(0.7, 1.3)
                units = max(0, round(base * trend * noise))
                revenue = round(units * price, 2)
                sales_rows.append((fsku, start, end, units, revenue, True, sr_sales))
                units_this_sku.append(units)
            per_sku_avg_units[fsku] = sum(units_this_sku) / len(units_this_sku)
            per_sku_avg_revenue[fsku] = sum(u * price for u in units_this_sku) / len(units_this_sku)

        psycopg2.extras.execute_values(
            fix_cur,
            "INSERT INTO sales_data (sku_id, period_start, period_end, units_sold, revenue, is_synthetic, source_record_id) VALUES %s",
            sales_rows,
        )
        print(f"sales_data: inserted {len(sales_rows)} rows ({len(priced) - skipped_no_fixture_match} priced SKUs x {N_MONTHS} months)")

        # ---- Generate margin_data (priced SKUs only)
        margin_rows = []
        for r in priced:
            fsku = fixture_sku_id_by_url.get(r["product_url"])
            if fsku is None:
                continue
            price = float(r["price"])
            unit_cost = round(price * random.uniform(0.35, 0.65), 2)
            margin_percent = round((price - unit_cost) / price * 100, 2)
            margin_rows.append((fsku, today, unit_cost, margin_percent, True, sr_margin))

        psycopg2.extras.execute_values(
            fix_cur,
            "INSERT INTO margin_data (sku_id, as_of_date, unit_cost, margin_percent, is_synthetic, source_record_id) VALUES %s",
            margin_rows,
        )
        print(f"margin_data: inserted {len(margin_rows)} rows")

        # ---- Generate channel_performance (priced SKUs only -- orders needs a price to derive from)
        channel_rows = []
        for r in priced:
            fsku = fixture_sku_id_by_url.get(r["product_url"])
            if fsku is None:
                continue
            price = float(r["price"])
            baseline_revenue = per_sku_avg_revenue.get(fsku, price * 10)

            noisy_weights = {ch: w * random.uniform(0.7, 1.3) for ch, w in CHANNELS.items()}
            total_weight = sum(noisy_weights.values())
            for ch, w in noisy_weights.items():
                share = w / total_weight
                ch_revenue = round(baseline_revenue * share, 2)
                orders = max(0, round(ch_revenue / price)) if price > 0 else 0
                return_rate = round(
                    random.uniform(8, 18) if ch in QCOMMERCE_CHANNELS else random.uniform(4, 10), 2
                )
                channel_rows.append((fsku, ch, ch_revenue, orders, return_rate, True, sr_channel))

        psycopg2.extras.execute_values(
            fix_cur,
            "INSERT INTO channel_performance (sku_id, channel, revenue, orders, return_rate, is_synthetic, source_record_id) VALUES %s",
            channel_rows,
        )
        print(f"channel_performance: inserted {len(channel_rows)} rows ({len(CHANNELS)} channels x priced SKUs)")

        # ---- Generate inventory_data (ALL SKUs, priced or not)
        inventory_rows = []
        for r in prod_skus:
            fsku = fixture_sku_id_by_url.get(r["product_url"])
            if fsku is None:
                continue
            is_oos = "out of stock" in (r["stock_status_tag"] or "").lower()
            current_stock = 0 if is_oos else random.randint(5, 200)
            avg_units = per_sku_avg_units.get(fsku)
            if avg_units and avg_units > 0:
                reorder_point = max(1, round(avg_units * 0.5))
                days_remaining = round(current_stock / (avg_units / 30), 1)
            else:
                reorder_point = 5
                days_remaining = None
            inventory_rows.append((fsku, today, current_stock, reorder_point, days_remaining, True, sr_inventory))

        psycopg2.extras.execute_values(
            fix_cur,
            "INSERT INTO inventory_data (sku_id, as_of_date, current_stock, reorder_point, days_of_stock_remaining, is_synthetic, source_record_id) VALUES %s",
            inventory_rows,
        )
        print(f"inventory_data: inserted {len(inventory_rows)} rows (all SKUs, priced or not)")

        # ---- Generate social_engagement (per brand/competitor_brand, brand-level not SKU-level)
        social_rows = []
        all_brand_entities = [("brand", name, bid) for name, bid in fixture_brand_id.items()] + \
                              [("competitor_brand", name, bid) for name, bid in fixture_competitor_id.items()]
        post_dates = []
        for start, end in periods:
            post_dates.append(start + datetime.timedelta(days=5))
            post_dates.append(start + datetime.timedelta(days=20))

        for kind, name, bid in all_brand_entities:
            activity = real_ad_activity.get(name)
            if activity and activity["ad_count"]:
                avg_days = float(activity["avg_days_running"] or 0)
                activity_level = min(10.0, 1 + float(activity["ad_count"]) * 0.5 + avg_days / 30)
                real_data_note = f"real: {activity['ad_count']} ad(s), avg {avg_days:.0f} days running"
            else:
                activity_level = 1.0
                real_data_note = "no real paid_ad_creative data for this brand -- baseline activity level used"

            for pd in post_dates:
                likes = round(activity_level * random.uniform(50, 400))
                comments = max(0, round(likes * random.uniform(0.02, 0.08)))
                shares = max(0, round(likes * random.uniform(0.01, 0.05)))
                platform = random.choice(["Instagram", "Facebook"])
                brand_id = bid if kind == "brand" else None
                competitor_brand_id = bid if kind == "competitor_brand" else None
                social_rows.append((brand_id, competitor_brand_id, pd, platform, likes, comments, shares, True, sr_social))

        psycopg2.extras.execute_values(
            fix_cur,
            "INSERT INTO social_engagement (brand_id, competitor_brand_id, post_date, platform, likes, comments, shares, is_synthetic, source_record_id) VALUES %s",
            social_rows,
        )
        print(f"social_engagement: inserted {len(social_rows)} rows ({len(all_brand_entities)} brands x {len(post_dates)} posts)")

        fix.commit()
        print("\nCOMMITTED.")

    except Exception:
        fix.rollback()
        print("ROLLED BACK -- no partial synthetic data left in fixture.")
        raise
    finally:
        prod.close()
        fix.close()


if __name__ == "__main__":
    main()
