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


def _launch_dates(cur, brand_names=None):
    where = f"{_BRAND_EXPR} = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        SELECT s.id AS sku_id, {_BRAND_EXPR} AS brand_name, s.product_name,
               sld.launch_date, sld.is_synthetic
        FROM sku_launch_data sld
        JOIN sku s ON s.id = sld.sku_id
        {_BRAND_JOIN}
        WHERE {where}
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _traffic_conversion(cur, brand_names=None):
    """Per-SKU total sessions (traffic_data) vs. total orders
    (channel_performance) -- conversion = orders / sessions. Both tables
    are independently generated (different random streams, neither derived
    from the other), so this ratio is not circular the way Q18's
    discount-vs-sales comparison is."""
    where = f"{_BRAND_EXPR} = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        WITH sessions AS (
            SELECT sku_id, SUM(sessions) AS total_sessions FROM traffic_data GROUP BY sku_id
        ),
        orders AS (
            SELECT sku_id, SUM(orders) AS total_orders FROM channel_performance GROUP BY sku_id
        )
        SELECT s.id AS sku_id, {_BRAND_EXPR} AS brand_name, s.product_name,
               p.price, sessions.total_sessions, orders.total_orders
        FROM sku s
        {_BRAND_JOIN}
        LEFT JOIN sessions ON sessions.sku_id = s.id
        LEFT JOIN orders ON orders.sku_id = s.id
        LEFT JOIN LATERAL (
            SELECT price FROM price_history ph WHERE ph.sku_id = s.id ORDER BY collected_at DESC LIMIT 1
        ) p ON TRUE
        WHERE {where}
        """,
        params,
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r["total_sessions"]:
            r["conversion_rate_pct"] = round(100.0 * (r["total_orders"] or 0) / r["total_sessions"], 2)
        else:
            r["conversion_rate_pct"] = None
    return rows


def _channel_inventory_agg(cur, brand_names=None):
    where = f"{_BRAND_EXPR} = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        SELECT s.id AS sku_id, {_BRAND_EXPR} AS brand_name, s.product_name,
               ci.channel, ci.stock, ci.days_of_stock
        FROM channel_inventory ci
        JOIN sku s ON s.id = ci.sku_id
        {_BRAND_JOIN}
        WHERE {where}
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _complaint_agg(cur, brand_names=None):
    where = f"{_BRAND_EXPR} = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        SELECT cd.channel, {_BRAND_EXPR} AS brand_name,
               SUM(cd.complaint_count) AS total_complaints,
               mode() WITHIN GROUP (ORDER BY cd.top_reason) AS most_common_reason
        FROM complaint_data cd
        JOIN sku s ON s.id = cd.sku_id
        {_BRAND_JOIN}
        WHERE {where}
        GROUP BY cd.channel, brand_name
        ORDER BY cd.channel, brand_name
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _ad_spend_agg(cur, brand_names=None):
    """Joins ad_spend_data (fabricated sku_id attribution) to its ad and,
    where matched, the SKU's real synthetic sales -- caller MUST surface
    the fabrication warning, this function does not hide it but also does
    not repeat it inline on every row."""
    where = f"COALESCE(b.name, cb.name) = ANY(%s)" if brand_names else "TRUE"
    params = [brand_names] if brand_names else []
    cur.execute(
        f"""
        SELECT pac.id AS ad_id, COALESCE(b.name, cb.name) AS brand_name, pac.theme,
               pac.product_subcategory, asd.sku_id, s.product_name,
               asd.spend, asd.impressions, asd.clicks, asd.attributed_orders
        FROM ad_spend_data asd
        JOIN paid_ad_creative pac ON pac.id = asd.paid_ad_creative_id
        LEFT JOIN brand b ON b.id = pac.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
        LEFT JOIN sku s ON s.id = asd.sku_id
        WHERE {where}
        ORDER BY asd.spend DESC
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
    launches = {r["sku_id"]: r for r in _launch_dates(cur, brand_names=[_OWN_BRAND])}
    metrics = {r["sku_id"]: r for r in _sku_metrics(cur, brand_names=[_OWN_BRAND])}
    sold_values = [r["units_sold"] for r in metrics.values() if r["units_sold"] is not None]
    avg_units = sum(sold_values) / len(sold_values) if sold_values else 0

    import datetime
    today = datetime.date.today()
    recent = []
    for sku_id, l in launches.items():
        m = metrics.get(sku_id)
        if m is None or m["units_sold"] is None:
            continue
        days_since_launch = (today - l["launch_date"]).days
        if days_since_launch <= 180 and m["units_sold"] < avg_units:
            recent.append({"product_name": m["product_name"], "launch_date": l["launch_date"],
                            "days_since_launch": days_since_launch, "units_sold": m["units_sold"],
                            "brand_avg_units_sold": round(avg_units, 1)})
    recent.sort(key=lambda r: r["units_sold"])
    return {
        "supported": "full",
        "data": recent[:10],
        "sources": [_src("sku_launch_data", "synthetic"), _src("sales_data", "synthetic")],
        "special_note": ("sku_launch_data.launch_date is entirely fabricated -- no real "
                          "launch-date field exists anywhere in this schema. This answer is "
                          "structurally sound (recent + below-average-sales), but the dates "
                          "themselves are demo placeholders, not real launch history."),
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
    rows = [r for r in _traffic_conversion(cur, brand_names=[_OWN_BRAND]) if r["conversion_rate_pct"] is not None]
    rates = [r["conversion_rate_pct"] for r in rows]
    avg_rate = sum(rates) / len(rates) if rates else 0
    flagged = [r for r in rows if r["conversion_rate_pct"] < avg_rate * 0.6 and r["price"] is not None]
    flagged.sort(key=lambda r: r["conversion_rate_pct"])
    return {
        "supported": "partial",
        "data": {"flagged_low_conversion_skus": flagged[:10], "brand_avg_conversion_rate_pct": round(avg_rate, 2)},
        "sources": [_src("traffic_data", "synthetic"), _src("channel_performance", "synthetic"), _src("price_history", "real")],
        "gap_explanation": ("The conversion ratio is computable (traffic_data / channel_performance "
                             "orders), but it is confounded with the sales generator's own "
                             "price/discount -> volume rule (see circularity note below), so this "
                             "cannot actually confirm or rule out a real pricing-conversion "
                             "relationship -- only real, independently-collected traffic data "
                             "could."),
        "special_note": ("CIRCULARITY WARNING: traffic_data's sessions (the denominator) are "
                          "generated independently of price, but orders (the numerator) come "
                          "from channel_performance, which is itself derived from sales_data -- "
                          "and sales_data's baseline units_sold was generated as a direct "
                          "function of each SKU's real price percentile and discount_percent "
                          "(cheaper/more-discounted SKUs get a higher baseline). So a SKU that "
                          "looks 'low-conversion' here very likely just has a lower generator "
                          "baseline (higher price, less discount), not an independently observed "
                          "conversion problem. This is largely restating the generator's built-in "
                          "price/discount -> volume rule, the same failure mode as Q18, not new "
                          "evidence that price is hurting conversion."),
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
    rows = _channel_inventory_agg(cur, brand_names=[_OWN_BRAND])
    flagged = [r for r in rows if r["stock"] is not None and r["stock"] < 5]
    flagged.sort(key=lambda r: r["stock"])
    return {
        "supported": "full",
        "data": flagged[:10],
        "sources": [_src("channel_inventory", "synthetic")],
        "special_note": ("channel_inventory.stock is a synthetic per-channel SPLIT of each SKU's "
                          "total inventory_data.current_stock (random weights) -- it is not an "
                          "independently observed per-channel stock count, since no real system "
                          "in this project tracks warehouse allocation by channel."),
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
    return_rows = {r["channel"]: r for r in _channel_agg(cur, brand_names=[_OWN_BRAND])}
    complaint_rows = {r["channel"]: r for r in _complaint_agg(cur, brand_names=[_OWN_BRAND])}
    merged = []
    for ch in set(return_rows) | set(complaint_rows):
        merged.append({
            "channel": ch,
            "avg_return_rate": return_rows.get(ch, {}).get("avg_return_rate"),
            "total_complaints": complaint_rows.get(ch, {}).get("total_complaints"),
            "most_common_complaint_reason": complaint_rows.get(ch, {}).get("most_common_reason"),
        })
    merged.sort(key=lambda r: (r["avg_return_rate"] or 0), reverse=True)
    return {
        "supported": "full",
        "data": merged,
        "sources": [_src("channel_performance", "synthetic"), _src("complaint_data", "synthetic")],
        "special_note": ("return_rate and complaint_count are generated INDEPENDENTLY (different "
                          "random streams) -- by design they can disagree on which channel is "
                          "'worst', the same way they can in reality. Report both, don't collapse "
                          "them into one number."),
    }


def q15_competitor_availability(cur):
    # Was: stock_status_tag ILIKE '%out%' OR actual_button_state ILIKE
    # '%disabled%' OR actual_button_state ILIKE '%sold%'. Dropped the
    # actual_button_state clauses -- naive substring matching on free-text
    # button-state descriptions false-positives on negated phrases: Borosil's
    # "Add to Cart (button active, not disabled)" contains "disabled" (61/63
    # rows wrongly counted unavailable, pushing Borosil to a false 100%);
    # Milton's "Unknown (no add-to-cart control or sold-out badge visible on
    # this card)" contains "sold" (14 rows wrongly counted unavailable, out
    # of 41 -> corrected to 27). Wonderchef and Prestige were unaffected
    # (their button-state text never contains those substrings). This is
    # entirely separate from the stock_mismatch column used by the confirmed
    # Milton/Borosil bug findings -- that logic lives in
    # competitor_collector.py's per-platform _is_mismatch functions, which
    # already use precise checks (e.g. actual_button_state.startswith("Add
    # to Cart")) specifically to avoid this exact substring trap.
    cur.execute(
        f"""
        SELECT {_BRAND_EXPR} AS brand_name,
               COUNT(*) AS total_skus,
               COUNT(*) FILTER (WHERE s.stock_status_tag ILIKE '%%out%%') AS unavailable_skus
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
    rows = _ad_spend_agg(cur, brand_names=[_OWN_BRAND])
    for r in rows:
        r["orders_per_1000_impressions"] = (round(1000.0 * r["attributed_orders"] / r["impressions"], 2)
                                             if r["impressions"] else None)
    rows.sort(key=lambda r: r["attributed_orders"], reverse=True)
    return {
        "supported": "partial",
        "data": rows,
        "gap_explanation": ("attributed_orders (and the sku_id each ad is linked to) come from "
                             "ad_spend_data, which FABRICATES the ad-to-product link -- "
                             "paid_ad_creative has no sku_id in reality, so which product each "
                             "real ad actually drove sales for is unknown. This ranks ads by a "
                             "synthetic attributed-orders figure, not a real, observed one."),
        "sources": [_src("paid_ad_creative", "real"), _src("ad_spend_data", "synthetic")],
    }


def q17_increase_ad_spend(cur):
    rows = _ad_spend_agg(cur, brand_names=[_OWN_BRAND])
    for r in rows:
        r["orders_per_rupee_spend"] = round(r["attributed_orders"] / float(r["spend"]), 4) if r["spend"] else None
    rows = [r for r in rows if r["sku_id"] is not None]
    rows.sort(key=lambda r: r["orders_per_rupee_spend"] or 0, reverse=True)
    return {
        "supported": "partial",
        "data": rows[:10],
        "gap_explanation": ("spend and attributed_orders are entirely synthetic (ad_spend_data), "
                             "and the sku_id each ad is linked to is a FABRICATED assignment, not "
                             "a real fact (paid_ad_creative has no spend or sku_id column in "
                             "reality). Any 'increase spend on X' conclusion drawn from this is "
                             "for demo purposes only -- there is no real baseline spend figure "
                             "behind it."),
        "sources": [_src("ad_spend_data", "synthetic")],
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
    ad_rows = _ad_spend_agg(cur, brand_names=[_OWN_BRAND])
    ad_orders_by_sku = {}
    for r in ad_rows:
        if r["sku_id"] is not None:
            ad_orders_by_sku[r["sku_id"]] = ad_orders_by_sku.get(r["sku_id"], 0) + r["attributed_orders"]

    scored = []
    for r in rows:
        if r["units_sold"] is None or r["margin_percent"] is None or r["days_of_stock_remaining"] is None:
            continue
        risk = 1.0 / max(float(r["days_of_stock_remaining"]), 1.0)
        marketing_boost = 1 + 0.1 * ad_orders_by_sku.get(r["sku_id"], 0)
        r["marketing_attributed_orders"] = ad_orders_by_sku.get(r["sku_id"], 0)
        r["priority_score"] = round(r["units_sold"] * float(r["margin_percent"]) / 100.0 * (1 + risk) * marketing_boost, 2)
        scored.append(r)
    scored.sort(key=lambda r: r["priority_score"], reverse=True)
    return {
        "supported": "partial",
        "data": scored[:5],
        "gap_explanation": ("Score combines sales momentum, margin, stock risk, and a marketing "
                             "leg from ad_spend_data.attributed_orders -- but that marketing leg "
                             "rests on a FABRICATED ad-to-SKU link (paid_ad_creative has no sku_id "
                             "in reality, so ad_spend_data invents which product each ad "
                             "'targets'). The sales/margin/stock legs are ordinary synthetic "
                             "data; the marketing leg specifically is demo scaffolding, not a "
                             "recovered fact, which is why this stays Partial rather than Full."),
        "sources": [_src("sales_data", "synthetic"), _src("margin_data", "synthetic"),
                    _src("inventory_data", "synthetic"), _src("ad_spend_data", "synthetic")],
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
        "special_note": ("NOISE-AS-SIGNAL WARNING: every SKU's period-over-period sales_data was "
                          "generated with the SAME tiny global trend (+/-3%/month) and only "
                          "independent per-period random noise (uniform 0.7-1.3x) on top. A SKU "
                          "flagged here got a run of unlucky noise draws, not a real business "
                          "decline -- this is a demonstration of what an early-warning detector "
                          "would look like on real data, not a genuine early-warning finding on "
                          "this demo data."),
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


def _incomplete_categories(cur, brand_name, categories):
    """Which of `categories` are confirmed incomplete for `brand_name`'s
    real, collected catalogue -- independent of whatever this specific
    question's SQL groups or filters by. Computed once per question run
    (cheap, one extra query) rather than threading a category-completeness
    check through every individual handler's own SQL, since almost every
    Nestasia-wide aggregate touches the full catalogue regardless of
    whether it happens to break results out by category."""
    cur.execute(
        """
        SELECT DISTINCT c.name FROM sku s
        JOIN brand b ON b.id = s.brand_id
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        WHERE b.name = %s AND c.name = ANY(%s) AND NOT s.collection_complete
        """,
        (brand_name, list(categories)),
    )
    return sorted(r["name"] for r in cur.fetchall())


def run_fixture_question(question_number):
    if question_number not in QUESTION_HANDLERS:
        raise ValueError(f"No handler for question_number={question_number!r}; valid range is 1-25.")
    conn, cur = _cur()
    try:
        result = QUESTION_HANDLERS[question_number](cur)
        result["incomplete_categories_included"] = _incomplete_categories(cur, _OWN_BRAND, ("Cookware", "Bakeware"))
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
