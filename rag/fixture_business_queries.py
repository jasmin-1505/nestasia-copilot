"""
Retrieval for the 25 fixed business questions in business_questions_25.md,
run ONLY against the FIXTURE database (synthetic sales/inventory/channel/
margin/social data, loaded by db/load_synthetic_business_data.py).

These 25 questions are a fixed, known set -- not open natural language --
so routing is by question NUMBER (1-25), not keyword classification.
retrieve.py's retrieve(question, db_mode="fixture", question_number=N)
dispatches here.

Every handler returns a dict with:
  - "supported": "full" | "partial" | "none"
  - "data": query results (list/dict), possibly empty
  - "gap_explanation": human-readable reason, present whenever supported
        is not "full" -- states plainly what part of the question this
        fixture schema cannot answer and why, rather than silently
        omitting or bluffing a proxy answer.
  - "sources": list of {"table": ..., "source_type": "synthetic"|"real"}
        describing exactly what fed the numbers, so generate.py's citation
        line never has to guess.
  - "special_note": optional extra honesty note (e.g. circularity warning,
        "competitor margin is unknowable in reality" warning).

No LLM is used anywhere in this file. Per the project's own established
lesson (see generate.py's _generate_templated / render_stock_mismatch_
aggregate_template history), letting a model freely narrate structured
multi-field evidence has repeatedly produced hallucinated or dropped
values. All 25 answers here are fully templated in generate.py from this
file's structured output.
"""
import psycopg2.extras

from retrieve import _connect

_BRAND_EXPR = "COALESCE(b.name, cb.name)"
_BRAND_JOIN = "LEFT JOIN brand b ON b.id = s.brand_id LEFT JOIN competitor_brand cb ON cb.id = s.competitor_brand_id"
_OWN_BRAND = "Nestasia"


def _cur():
    conn = _connect("FIXTURE")
    return conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _sku_metrics(cur, brand_names=None, category_names=None, sku_ids=None):
    """One row per SKU, LEFT JOINed against latest inventory/margin/price and
    summed sales -- a missing synthetic value comes back as SQL NULL /
    Python None, never coerced to 0. This is what makes the 10 excluded
    unpriced SKUs surface as None instead of silently vanishing or reading
    as zero sales."""
    where = ["TRUE"]
    params = []
    if brand_names:
        where.append(f"{_BRAND_EXPR} = ANY(%s)")
        params.append(brand_names)
    if category_names:
        where.append("c.name = ANY(%s)")
        params.append(category_names)
    if sku_ids:
        where.append("s.id = ANY(%s)")
        params.append(sku_ids)
    where_sql = " AND ".join(where)
    cur.execute(
        f"""
        WITH sales AS (
            SELECT sku_id, SUM(units_sold) AS units_sold, SUM(revenue) AS revenue,
                   COUNT(DISTINCT period_start) AS n_periods,
                   bool_or(is_synthetic) AS is_synthetic
            FROM sales_data GROUP BY sku_id
        ),
        inv AS (
            SELECT DISTINCT ON (sku_id) sku_id, current_stock, reorder_point,
                   days_of_stock_remaining, is_synthetic
            FROM inventory_data ORDER BY sku_id, as_of_date DESC
        ),
        marg AS (
            SELECT DISTINCT ON (sku_id) sku_id, unit_cost, margin_percent, is_synthetic
            FROM margin_data ORDER BY sku_id, as_of_date DESC
        ),
        price AS (
            SELECT DISTINCT ON (sku_id) sku_id, price, discount_percent, collected_at
            FROM price_history ORDER BY sku_id, collected_at DESC
        )
        SELECT s.id AS sku_id, {_BRAND_EXPR} AS brand_name, s.product_name,
               c.name AS category_name, s.stock_status_tag, s.created_at,
               sales.units_sold, sales.revenue, sales.n_periods, sales.is_synthetic AS sales_is_synthetic,
               inv.current_stock, inv.reorder_point, inv.days_of_stock_remaining, inv.is_synthetic AS inv_is_synthetic,
               marg.unit_cost, marg.margin_percent, marg.is_synthetic AS margin_is_synthetic,
               price.price, price.discount_percent
        FROM sku s
        {_BRAND_JOIN}
        LEFT JOIN sku_category sc ON sc.sku_id = s.id
        LEFT JOIN category c ON c.id = sc.category_id
        LEFT JOIN sales ON sales.sku_id = s.id
        LEFT JOIN inv ON inv.sku_id = s.id
        LEFT JOIN marg ON marg.sku_id = s.id
        LEFT JOIN price ON price.sku_id = s.id
        WHERE {where_sql}
        ORDER BY brand_name, s.product_name
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _channel_agg(cur, brand_names=None, channel=None):
    where = ["TRUE"]
    params = []
    if brand_names:
        where.append(f"{_BRAND_EXPR} = ANY(%s)")
        params.append(brand_names)
    if channel:
        where.append("cp.channel = %s")
        params.append(channel)
    where_sql = " AND ".join(where)
    cur.execute(
        f"""
        SELECT cp.channel, {_BRAND_EXPR} AS brand_name,
               SUM(cp.revenue) AS revenue, SUM(cp.orders) AS orders,
               ROUND(AVG(cp.return_rate), 4) AS avg_return_rate,
               bool_or(cp.is_synthetic) AS is_synthetic
        FROM channel_performance cp
        JOIN sku s ON s.id = cp.sku_id
        {_BRAND_JOIN}
        WHERE {where_sql}
        GROUP BY cp.channel, brand_name
        ORDER BY cp.channel, brand_name
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _distinct_channels(cur):
    cur.execute("SELECT DISTINCT channel FROM channel_performance ORDER BY channel")
    return [r["channel"] for r in cur.fetchall()]


def _social_agg(cur):
    cur.execute(
        """
        SELECT COALESCE(b.name, cb.name) AS brand_name, se.platform,
               SUM(se.likes) AS likes, SUM(se.comments) AS comments, SUM(se.shares) AS shares,
               COUNT(*) AS n_posts, bool_or(se.is_synthetic) AS is_synthetic
        FROM social_engagement se
        LEFT JOIN brand b ON b.id = se.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = se.competitor_brand_id
        GROUP BY brand_name, se.platform
        ORDER BY brand_name, se.platform
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _ad_agg(cur, brand_names=None):
    where = f"COALESCE(b.name, cb.name) = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        SELECT COALESCE(b.name, cb.name) AS brand_name, pac.theme, pac.offer_discount,
               COUNT(*) AS n_ads, ROUND(AVG(pac.days_running), 1) AS avg_days_running
        FROM paid_ad_creative pac
        LEFT JOIN brand b ON b.id = pac.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
        WHERE {where}
        GROUP BY brand_name, pac.theme, pac.offer_discount
        ORDER BY brand_name, n_ads DESC
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


SOURCE_SYNTHETIC = {"table": None, "source_type": "synthetic"}
SOURCE_REAL = {"table": None, "source_type": "real"}


def _src(table, source_type):
    return {"table": table, "source_type": source_type}


# ---------------------------------------------------------------------------
# One handler per question number. Each opens its own connection so the 25
# can be run and reported independently (a failure in one doesn't take
# down the rest).
# ---------------------------------------------------------------------------

def q1_prioritize_promotion(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    ranked = sorted([r for r in rows if r["units_sold"] is not None], key=lambda r: r["units_sold"], reverse=True)[:5]
    return {
        "supported": "full",
        "data": ranked,
        "sources": [_src("sales_data", "synthetic"), _src("inventory_data", "synthetic")],
    }


def q2_slow_moving_inventory(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    flagged = [r for r in rows if r["current_stock"] is not None and r["units_sold"] is not None
               and r["current_stock"] > 0 and r["units_sold"] < r["current_stock"] * 0.1]
    flagged.sort(key=lambda r: r["current_stock"], reverse=True)
    return {
        "supported": "full",
        "data": flagged[:10],
        "sources": [_src("sales_data", "synthetic"), _src("inventory_data", "synthetic")],
    }


def q3_strongest_category_vs_competitors(cur):
    rows = _sku_metrics(cur)
    by_cat_brand = {}
    for r in rows:
        if r["category_name"] is None or r["revenue"] is None:
            continue
        is_own = r["brand_name"] == _OWN_BRAND
        key = (r["category_name"], "own" if is_own else "competitor")
        by_cat_brand.setdefault(key, 0)
        by_cat_brand[key] += float(r["revenue"])
    return {
        "supported": "full",
        "data": [{"category": k[0], "side": k[1], "total_revenue": round(v, 2)} for k, v in by_cat_brand.items()],
        "sources": [_src("sales_data", "synthetic")],
    }


def q4_bestsellers_at_risk(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    flagged = [r for r in rows if r["units_sold"] and r["days_of_stock_remaining"] is not None
               and r["days_of_stock_remaining"] < 14]
    flagged.sort(key=lambda r: r["units_sold"], reverse=True)
    return {
        "supported": "full",
        "data": flagged[:10],
        "sources": [_src("sales_data", "synthetic"), _src("inventory_data", "synthetic")],
    }


def q5_recent_launches(cur):
    return {
        "supported": "none",
        "data": [],
        "gap_explanation": ("No table tracks a true product-launch date. sku.created_at only "
                             "records when this project's collector first inserted the row -- "
                             "it reflects OUR crawl history, not when the product actually "
                             "launched on the brand's site. Answering this would require a "
                             "real launch-date field, which does not exist in this schema."),
        "sources": [],
    }


def q6_underpricing_vs_competitors(cur):
    rows = _sku_metrics(cur, category_names=None)
    by_cat_brand = {}
    for r in rows:
        if r["category_name"] is None or r["price"] is None:
            continue
        key = (r["category_name"], r["brand_name"])
        by_cat_brand.setdefault(key, []).append(float(r["price"]))
    price_avgs = {k: sum(v) / len(v) for k, v in by_cat_brand.items()}
    margin_rows = [r for r in rows if r["brand_name"] == _OWN_BRAND and r["margin_percent"] is not None]
    return {
        "supported": "partial",
        "data": {
            "own_margin_by_sku": [{"product_name": r["product_name"], "category_name": r["category_name"],
                                    "margin_percent": r["margin_percent"], "price": r["price"]}
                                   for r in margin_rows],
            "avg_price_by_category_brand": [{"category": k[0], "brand": k[1], "avg_price": round(v, 2)}
                                             for k, v in price_avgs.items()],
        },
        "gap_explanation": ("Our own margin_percent is a synthetic estimate, not a real cost "
                             "figure. Competitor prices shown are real (price_history), but "
                             "competitor margin_percent -- also present in this fixture -- is "
                             "NOT used here to claim 'they have more margin than us': a "
                             "company's actual cost structure is not public information, so any "
                             "competitor margin figure in this demo is a synthetic placeholder, "
                             "never a real finding, no matter how it's phrased."),
        "sources": [_src("margin_data", "synthetic"), _src("price_history", "real")],
        "special_note": ("Competitor margin/cost data is fundamentally unknowable from public "
                          "sources in real life -- treat any competitor margin_percent value in "
                          "this fixture as illustrative scaffolding for the demo UI, never as a "
                          "claim about the real business."),
    }


def q7_best_margin_to_volume(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    scored = [r for r in rows if r["margin_percent"] is not None and r["units_sold"] is not None]
    for r in scored:
        r["margin_volume_score"] = round(float(r["margin_percent"]) * r["units_sold"], 2)
    scored.sort(key=lambda r: r["margin_volume_score"], reverse=True)
    return {
        "supported": "full",
        "data": scored[:10],
        "sources": [_src("margin_data", "synthetic"), _src("sales_data", "synthetic")],
    }


def q8_promotion_to_clear_inventory(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    by_cat = {}
    for r in rows:
        if r["category_name"] is None:
            continue
        d = by_cat.setdefault(r["category_name"], {"total_stock": 0, "total_units_sold": 0})
        d["total_stock"] += r["current_stock"] or 0
        d["total_units_sold"] += r["units_sold"] or 0
    out = [{"category": k, **v, "stock_to_sales_ratio": round(v["total_stock"] / v["total_units_sold"], 2)
            if v["total_units_sold"] else None} for k, v in by_cat.items()]
    out.sort(key=lambda r: (r["stock_to_sales_ratio"] is None, -(r["stock_to_sales_ratio"] or 0)))
    return {
        "supported": "full",
        "data": out,
        "sources": [_src("inventory_data", "synthetic"), _src("sales_data", "synthetic")],
    }


def q9_profitability_cookware_vs_bakeware(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND], category_names=["Cookware", "Bakeware"])
    by_cat = {}
    for r in rows:
        if r["margin_percent"] is None:
            continue
        by_cat.setdefault(r["category_name"], []).append(float(r["margin_percent"]))
    out = [{"category": k, "avg_margin_percent": round(sum(v) / len(v), 2), "n_skus": len(v)} for k, v in by_cat.items()]
    return {
        "supported": "full",
        "data": out,
        "sources": [_src("margin_data", "synthetic")],
    }


def q10_pricing_hurting_conversion(cur):
    return {
        "supported": "none",
        "data": [],
        "gap_explanation": ("No table anywhere in this schema tracks visits, sessions, or page "
                             "traffic. channel_performance has 'orders' but no denominator "
                             "(visits/sessions) to compute a conversion rate from -- there is "
                             "literally no numerator/denominator pair available. Answering this "
                             "would require adding a traffic or session-count field."),
        "sources": [],
    }


def q11_channel_driving_revenue(cur):
    rows = _channel_agg(cur, brand_names=[_OWN_BRAND])
    rows.sort(key=lambda r: r["revenue"] or 0, reverse=True)
    return {
        "supported": "full",
        "data": rows,
        "sources": [_src("channel_performance", "synthetic")],
    }


def q12_out_of_stock_specific_channel(cur):
    channels = _distinct_channels(cur)
    high_demand = [r for r in _sku_metrics(cur, brand_names=[_OWN_BRAND])
                   if r["units_sold"] and r["current_stock"] is not None and r["current_stock"] < 5]
    return {
        "supported": "partial",
        "data": {"low_stock_high_demand_skus": high_demand[:10], "known_channels": channels},
        "gap_explanation": ("inventory_data tracks stock PER-SKU ONLY, with no channel column -- "
                             "there is no way to know whether a given unit of stock sits in the "
                             "warehouse feeding one channel vs. another. The SKUs listed are low "
                             "on OVERALL stock and selling well, but which channel(s) would "
                             "actually see it go out of stock first cannot be determined from "
                             "this schema."),
        "sources": [_src("inventory_data", "synthetic"), _src("sales_data", "synthetic")],
    }


_QCOMMERCE_CHANNELS = ("Blinkit", "Instamart", "Zepto")


def q13_expand_qcommerce(cur):
    channels = _distinct_channels(cur)
    qc_present = [c for c in _QCOMMERCE_CHANNELS if c in channels]
    if not qc_present:
        return {
            "supported": "none",
            "data": {"known_channels": channels},
            "gap_explanation": (f"No quick-commerce channel (Blinkit/Instamart/Zepto) is present. "
                                 f"The channels actually present in this fixture are: "
                                 f"{', '.join(c for c in channels if c)}."),
            "sources": [_src("channel_performance", "synthetic")],
        }
    rows = [r for r in _channel_agg(cur, brand_names=[_OWN_BRAND]) if r["channel"] in qc_present]
    return {
        "supported": "full",
        "data": rows,
        "gap_explanation": ("No literal 'qCommerce' channel label exists in this schema -- "
                             f"interpreted as the three quick-commerce channels present: "
                             f"{', '.join(qc_present)}. This is a naming interpretation, not a "
                             f"data gap."),
        "sources": [_src("channel_performance", "synthetic")],
    }


def q14_highest_return_rate(cur):
    rows = _channel_agg(cur, brand_names=[_OWN_BRAND])
    rows.sort(key=lambda r: r["avg_return_rate"] or 0, reverse=True)
    return {
        "supported": "partial",
        "data": rows,
        "gap_explanation": ("channel_performance has 'return_rate' but no separate 'complaint' "
                             "or customer-service-ticket field. This answer covers RETURN RATE "
                             "only -- it does not know whether complaint volume tracks returns "
                             "1:1 (a customer can complain without returning, or return without "
                             "complaining)."),
        "sources": [_src("channel_performance", "synthetic")],
    }


def q15_competitor_availability(cur):
    cur.execute(
        f"""
        SELECT {_BRAND_EXPR} AS brand_name,
               COUNT(*) AS total_skus,
               COUNT(*) FILTER (WHERE s.stock_status_tag ILIKE '%%out%%' OR s.actual_button_state ILIKE '%%disabled%%'
                                 OR s.actual_button_state ILIKE '%%sold%%') AS unavailable_skus
        FROM sku s
        {_BRAND_JOIN}
        GROUP BY brand_name
        ORDER BY brand_name
        """
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["unavailable_pct"] = round(100.0 * r["unavailable_skus"] / r["total_skus"], 1) if r["total_skus"] else None
    return {
        "supported": "full",
        "data": rows,
        "sources": [_src("sku", "real")],
    }


def q16_ad_campaign_driving_sales(cur):
    ads = _ad_agg(cur)
    sales = _sku_metrics(cur)
    sales_by_brand = {}
    for r in sales:
        if r["revenue"] is None:
            continue
        sales_by_brand[r["brand_name"]] = sales_by_brand.get(r["brand_name"], 0) + float(r["revenue"])
    return {
        "supported": "partial",
        "data": {"ad_activity_by_brand_theme": ads,
                  "total_synthetic_revenue_by_brand": [{"brand": k, "revenue": round(v, 2)} for k, v in sales_by_brand.items()]},
        "gap_explanation": ("paid_ad_creative has NO sku_id column -- ads link only to a brand, "
                             "never to an individual product. There is also no per-ad sales "
                             "attribution window (most ads have end_date=NULL, i.e. still "
                             "'Active', so there's no clean before/after period to compare "
                             "against). This can show ad activity next to a brand's total "
                             "synthetic sales, but CANNOT attribute any specific sale to any "
                             "specific campaign."),
        "sources": [_src("paid_ad_creative", "real"), _src("sales_data", "synthetic")],
    }


def q17_increase_ad_spend(cur):
    return {
        "supported": "none",
        "data": [],
        "gap_explanation": ("paid_ad_creative has no spend/budget column at all, and no sku_id "
                             "to link an ad to a product. There is no baseline spend figure to "
                             "reference, so a spend-increase recommendation cannot be grounded "
                             "in this data at all -- not even partially."),
        "sources": [],
    }


def q18_festive_discount_campaigns(cur):
    rows = [r for r in _sku_metrics(cur, brand_names=[_OWN_BRAND])
            if r["units_sold"] is not None and r["discount_percent"] is not None]
    discounted = [r for r in rows if r["discount_percent"] and r["discount_percent"] > 0]
    full_price = [r for r in rows if not r["discount_percent"]]
    avg_disc = sum(r["units_sold"] for r in discounted) / len(discounted) if discounted else None
    avg_full = sum(r["units_sold"] for r in full_price) / len(full_price) if full_price else None
    return {
        "supported": "partial",
        "data": {"avg_units_sold_discounted_skus": round(avg_disc, 1) if avg_disc is not None else None,
                  "avg_units_sold_full_price_skus": round(avg_full, 1) if avg_full is not None else None,
                  "n_discounted": len(discounted), "n_full_price": len(full_price)},
        "gap_explanation": ("IMPORTANT CIRCULARITY WARNING: the synthetic sales figures were "
                             "themselves generated using each SKU's real discount_percent as an "
                             "input signal (see db/load_synthetic_business_data.py). So finding "
                             "'discounted SKUs sell more' here is confirming the generator's own "
                             "assumption, not an independent discovery -- it would be circular "
                             "to present this as new evidence that discounting works. There is "
                             "also no separate 'festive campaign' flag distinct from ordinary "
                             "discount_percent, so festive vs. everyday-discount cannot be told "
                             "apart at all."),
        "sources": [_src("price_history", "real"), _src("sales_data", "synthetic")],
    }


def q19_social_engagement_what_to_promote(cur):
    rows = _social_agg(cur)
    own = [r for r in rows if r["brand_name"] == _OWN_BRAND]
    return {
        "supported": "partial",
        "data": own,
        "gap_explanation": ("social_engagement rows are BRAND-level only (brand_id/"
                             "competitor_brand_id + platform), with no product or theme "
                             "linkage. This can show which platform gets the most engagement "
                             "for Nestasia overall, but cannot say which specific PRODUCT to "
                             "promote next based on social data."),
        "sources": [_src("social_engagement", "synthetic")],
    }


def q20_competitor_promotional_approach(cur):
    ads = _ad_agg(cur)
    competitor_ads = [r for r in ads if r["brand_name"] != _OWN_BRAND]
    competitor_ads.sort(key=lambda r: r["n_ads"], reverse=True)
    return {
        "supported": "partial",
        "data": competitor_ads,
        "gap_explanation": ("This reports which competitor themes/offers appear most FREQUENTLY "
                             "(real ad-library data) -- it does not measure 'effectiveness' "
                             "(there is no sales attribution to any specific ad, for us or for "
                             "competitors). Read this as an activity ranking, not a proven "
                             "effectiveness ranking."),
        "sources": [_src("paid_ad_creative", "real")],
    }


def q21_prioritize_single_product(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    scored = []
    for r in rows:
        if r["units_sold"] is None or r["margin_percent"] is None or r["days_of_stock_remaining"] is None:
            continue
        risk = 1.0 / max(float(r["days_of_stock_remaining"]), 1.0)
        r["priority_score"] = round(r["units_sold"] * float(r["margin_percent"]) / 100.0 * (1 + risk), 2)
        scored.append(r)
    scored.sort(key=lambda r: r["priority_score"], reverse=True)
    return {
        "supported": "partial",
        "data": scored[:5],
        "gap_explanation": ("Score combines sales momentum, margin, and stock risk (all "
                             "synthetic). It does NOT factor in marketing/ad performance at the "
                             "product level, because paid_ad_creative has no sku_id -- ads "
                             "cannot be linked to individual products at all, so the "
                             "'marketing' leg of this cross-cutting question is structurally "
                             "unanswerable per-SKU."),
        "sources": [_src("sales_data", "synthetic"), _src("margin_data", "synthetic"), _src("inventory_data", "synthetic")],
    }


def q22_fix_one_thing_kitchen(cur):
    rows = _sku_metrics(cur, brand_names=[_OWN_BRAND])
    by_cat = {}
    for r in rows:
        if r["category_name"] is None:
            continue
        d = by_cat.setdefault(r["category_name"], {"total_stock": 0, "total_units_sold": 0, "margins": []})
        d["total_stock"] += r["current_stock"] or 0
        d["total_units_sold"] += r["units_sold"] or 0
        if r["margin_percent"] is not None:
            d["margins"].append(float(r["margin_percent"]))
    out = [{"category": k, "total_stock": v["total_stock"], "total_units_sold": v["total_units_sold"],
            "avg_margin_percent": round(sum(v["margins"]) / len(v["margins"]), 2) if v["margins"] else None}
           for k, v in by_cat.items()]
    return {
        "supported": "full",
        "data": out,
        "gap_explanation": ("There is no category literally named 'Kitchen' in this schema -- "
                             "the 5 tracked categories are Bakeware, Container, Cookware, "
                             "Kitchen Racks+Trivets, and Lunch Boxes+Bags. 'Kitchen' is "
                             "interpreted here as ALL tracked categories combined, so the "
                             "figures below break out by each real category rather than one "
                             "single 'Kitchen' bucket. This is an interpretation choice, not a "
                             "data gap."),
        "sources": [_src("sales_data", "synthetic"), _src("inventory_data", "synthetic"), _src("margin_data", "synthetic")],
    }


def q23_quietly_becoming_problem(cur):
    cur.execute(
        f"""
        SELECT s.id AS sku_id, {_BRAND_EXPR} AS brand_name, s.product_name,
               sd.period_start, sd.period_end, sd.units_sold, sd.is_synthetic
        FROM sales_data sd JOIN sku s ON s.id = sd.sku_id
        {_BRAND_JOIN}
        WHERE {_BRAND_EXPR} = %s
        ORDER BY s.id, sd.period_start
        """,
        [_OWN_BRAND],
    )
    rows = [dict(r) for r in cur.fetchall()]
    by_sku = {}
    for r in rows:
        by_sku.setdefault(r["sku_id"], {"product_name": r["product_name"], "periods": []})
        by_sku[r["sku_id"]]["periods"].append(r["units_sold"])
    declining = []
    for sku_id, d in by_sku.items():
        periods = d["periods"]
        if len(periods) >= 2 and periods[-1] < periods[0] * 0.7:
            declining.append({"sku_id": sku_id, "product_name": d["product_name"],
                               "units_sold_by_period": periods})
    if not declining and not any(len(d["periods"]) >= 2 for d in by_sku.values()):
        return {
            "supported": "none",
            "data": [],
            "gap_explanation": "Each SKU has only a single sales_data period in this fixture, so no period-over-period trend can be computed at all.",
            "sources": [_src("sales_data", "synthetic")],
        }
    return {
        "supported": "full",
        "data": declining[:10],
        "sources": [_src("sales_data", "synthetic")],
    }


def q24_vulnerable_to_competitor(cur):
    rows = _sku_metrics(cur)
    by_cat_brand = {}
    for r in rows:
        if r["category_name"] is None or r["revenue"] is None:
            continue
        key = (r["category_name"], r["brand_name"])
        by_cat_brand.setdefault(key, 0)
        by_cat_brand[key] += float(r["revenue"])
    by_cat = {}
    for (cat, brand), rev in by_cat_brand.items():
        by_cat.setdefault(cat, {})[brand] = rev
    vulnerable = []
    for cat, brand_rev in by_cat.items():
        own = brand_rev.get(_OWN_BRAND, 0)
        competitors = {b: v for b, v in brand_rev.items() if b != _OWN_BRAND}
        if competitors:
            top_comp, top_val = max(competitors.items(), key=lambda kv: kv[1])
            if top_val > own:
                vulnerable.append({"category": cat, "own_revenue": round(own, 2),
                                    "top_competitor": top_comp, "top_competitor_revenue": round(top_val, 2)})
    vulnerable.sort(key=lambda r: r["top_competitor_revenue"] - r["own_revenue"], reverse=True)
    return {
        "supported": "full",
        "data": vulnerable,
        "sources": [_src("sales_data", "synthetic")],
    }


def q25_cut_ten_percent_skus(cur):
    rows = [r for r in _sku_metrics(cur, brand_names=[_OWN_BRAND])
            if r["units_sold"] is not None and r["margin_percent"] is not None]
    for r in rows:
        r["cut_candidate_score"] = round(r["units_sold"] * float(r["margin_percent"]), 2)
    rows.sort(key=lambda r: r["cut_candidate_score"])
    n_cut = max(1, round(len(rows) * 0.10))
    return {
        "supported": "full",
        "data": rows[:n_cut],
        "sources": [_src("sales_data", "synthetic"), _src("margin_data", "synthetic")],
    }


QUESTION_HANDLERS = {
    1: q1_prioritize_promotion, 2: q2_slow_moving_inventory, 3: q3_strongest_category_vs_competitors,
    4: q4_bestsellers_at_risk, 5: q5_recent_launches, 6: q6_underpricing_vs_competitors,
    7: q7_best_margin_to_volume, 8: q8_promotion_to_clear_inventory, 9: q9_profitability_cookware_vs_bakeware,
    10: q10_pricing_hurting_conversion, 11: q11_channel_driving_revenue, 12: q12_out_of_stock_specific_channel,
    13: q13_expand_qcommerce, 14: q14_highest_return_rate, 15: q15_competitor_availability,
    16: q16_ad_campaign_driving_sales, 17: q17_increase_ad_spend, 18: q18_festive_discount_campaigns,
    19: q19_social_engagement_what_to_promote, 20: q20_competitor_promotional_approach,
    21: q21_prioritize_single_product, 22: q22_fix_one_thing_kitchen, 23: q23_quietly_becoming_problem,
    24: q24_vulnerable_to_competitor, 25: q25_cut_ten_percent_skus,
}


def run_fixture_question(question_number):
    if question_number not in QUESTION_HANDLERS:
        raise ValueError(f"No handler for question_number={question_number!r}; valid range is 1-25.")
    conn, cur = _cur()
    try:
        result = QUESTION_HANDLERS[question_number](cur)
    finally:
        cur.close()
        conn.close()
    result.setdefault("gap_explanation", None)
    result.setdefault("special_note", None)
    result["question_number"] = question_number
    result["db_mode"] = "fixture"
    return result


def test_excluded_sku_surfaces_gap():
    """Picks one of the 10 real-catalogue SKUs with no ever-recorded price
    (correctly excluded from sales_data/margin_data/channel_performance by
    the loader) and confirms _sku_metrics() returns None for its
    sales/margin fields -- NOT zero, NOT a missing row."""
    conn, cur = _cur()
    try:
        cur.execute(
            """
            SELECT s.id FROM sku s
            WHERE NOT EXISTS (SELECT 1 FROM price_history ph WHERE ph.sku_id = s.id AND ph.price IS NOT NULL)
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            return {"ok": False, "reason": "No unpriced SKU found in fixture -- cannot run this test."}
        sku_id = row["id"]
        metrics = _sku_metrics(cur, sku_ids=[sku_id])
        if not metrics:
            return {"ok": False, "reason": f"sku_id={sku_id} returned no row at all -- it was silently dropped, which is exactly the failure mode to avoid."}
        m = metrics[0]
        surfaced = m["units_sold"] is None and m["revenue"] is None and m["margin_percent"] is None
        return {
            "ok": surfaced,
            "sku_id": sku_id,
            "product_name": m["product_name"],
            "units_sold": m["units_sold"],
            "revenue": m["revenue"],
            "margin_percent": m["margin_percent"],
            "message": ("no sales/margin data available for this SKU" if surfaced else
                        "FAILED: expected None for units_sold/revenue/margin_percent"),
        }
    finally:
        cur.close()
        conn.close()
