"""
Presentation layer -- turns a question's raw structured data into a small,
UI-agnostic bundle: headline, visual, suggestion, firm_up, sources. No LLM
anywhere in this file. app.py renders the bundle; test_presenters.py
verifies every number quoted in a headline traces back to the underlying
data (never a hallucinated or rounded-wrong figure).

Each presenter function takes the raw retrieval result (whatever
fixture_business_queries.run_fixture_question() or retrieve.retrieve()
already returns -- no re-fetching, no re-deriving) and returns:

    {
        "headline": str,          # one sentence, f-string from computed
                                   # values only
        "visual": {...} | None,   # {"type": "table", "columns": [...],
                                   #  "rows": [...]}  or
                                   # {"type": "bar_chart", "x_label": ...,
                                   #  "y_label": ..., "categories": [...],
                                   #  "values": [...]}
        "suggestion": str,        # one hedged line; decision-shaped
                                   # questions use "candidates, ranked by
                                   # <metric>", never "you should"
        "firm_up": str,           # what real data/step would confirm this
        "sources": [ {"label": str, "url": str|None, "as_of": str|None,
                       "kind": "real"|"demo"} ],
    }

Live presenters call retrieve.retrieve() directly (NOT generate()) --
deliberately bypassing the Ollama LLM path entirely for the primary card,
since a presenter's headline must be an f-string built from computed
values, never model prose. This also happens to fix two real defects found
in the Step-0 screenshot review: the LLM's answer text was leaking
internal variable names ("own_brand"/"tracked_competitors") and a stray
markdown/code-span artifact into user-facing text.
"""
import datetime

from retrieve import retrieve

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
_LAKH = 1_00_000
_CRORE = 1_00_00_000


def _indian_grouping(n):
    n = int(round(n))
    s = str(abs(n))
    if len(s) <= 3:
        out = s
    else:
        last3, rest = s[-3:], s[:-3]
        parts = []
        while len(rest) > 2:
            parts.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            parts.insert(0, rest)
        out = ",".join(parts) + "," + last3
    return ("-" if n < 0 else "") + out


def format_inr(amount):
    """₹ with Indian digit grouping below 1 lakh, lakh/crore above it."""
    if amount is None:
        return "n/a"
    amount = float(amount)
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    if a >= _CRORE:
        return f"{sign}₹{a / _CRORE:.2f} crore"
    if a >= _LAKH:
        return f"{sign}₹{a / _LAKH:.2f} lakh"
    return f"₹{_indian_grouping(amount)}"


def format_date(value):
    """'26 Sep 2026' from a date/datetime/ISO-string. None-safe."""
    if value is None:
        return "n/a"
    if isinstance(value, str):
        try:
            value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime("%d %b %Y")


def format_pct(value):
    if value is None:
        return "n/a"
    return f"{float(value):.1f}%"


# ---------------------------------------------------------------------------
# Shared source-list builders
# ---------------------------------------------------------------------------
def _demo_sources(tables):
    return [{"label": f"{t} (fixture)", "url": None, "as_of": None, "kind": "demo"} for t in tables]


def _real_sources_from_rows(rows, url_field="product_url", label_field="product_name"):
    out = []
    for r in rows:
        out.append({
            "label": r.get(label_field, "source"),
            "url": r.get(url_field),
            "as_of": format_date(r.get("collected_at")),
            "kind": "real",
        })
    return out


# ---------------------------------------------------------------------------
# Demo presenters -- operate on fixture_business_queries.run_fixture_question(n)'s
# return dict directly.
# ---------------------------------------------------------------------------
def _bar(labels, values, unit, kind="demo"):
    """The one concrete bar-chart shape every presenter uses."""
    return {"type": "bar", "labels": labels, "values": values, "unit": unit, "kind": kind}


def _table(columns, rows, kind="demo"):
    """The one concrete table shape. `columns` is a list of plain strings
    for a single-source table, or a list of {"label":..., "kind":"real"|
    "demo"} dicts when the table blends real and demo data in different
    columns -- Step 3's requirement that a blended table tag each column."""
    cols = [{"label": c, "kind": kind} if isinstance(c, str) else c for c in columns]
    return {"type": "table", "columns": cols, "rows": rows}


def _no_data_bundle(raw, reason):
    return {
        "headline": f"No candidates found ({reason}).",
        "visual": None,
        "suggestion": "Nothing to rank here right now.",
        "firm_up": raw.get("gap_explanation") or "N/A",
        "sources": [],
        "caveat": None,
    }


def present_demo_1(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no SKUs with recorded sales")
    top = rows[0]
    headline = (f"{top['product_name']} leads with {top['units_sold']} units sold "
                f"({format_inr(top['revenue'])} revenue).")
    visual = _bar([r["product_name"][:28] for r in rows], [r["units_sold"] for r in rows], "units")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by units sold. Which to actually promote is a decision for your team.",
        "firm_up": "Confirm against real sales_data once your POS/e-commerce sales feed is connected -- these units_sold figures are synthetic placeholders.",
        "sources": _demo_sources(["sales_data", "inventory_data"]),
        "caveat": None,
    }


def present_demo_2(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no slow-moving SKUs found")
    top = rows[0]
    headline = (f"{len(rows)} SKU(s) look slow-moving -- {top['product_name']} has "
                f"{top['current_stock']} units in stock against only {top['units_sold']} sold.")
    visual = _table(["Product", "Current stock", "Units sold"],
                     [[r["product_name"], r["current_stock"], r["units_sold"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by current stock. Whether to discount or hold is a decision for your team.",
        "firm_up": "Confirm current_stock and units_sold against real inventory/sales systems.",
        "sources": _demo_sources(["sales_data", "inventory_data"]),
        "caveat": None,
    }


def present_demo_3(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no category revenue recorded")
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"], {}).setdefault(r["side"], r["total_revenue"])
    best_cat, best_margin = None, None
    for cat, sides in by_cat.items():
        own, comp = sides.get("own"), sides.get("competitor")
        if own is not None and comp is not None:
            margin = own - comp
            if best_margin is None or margin > best_margin:
                best_margin, best_cat = margin, cat
    if best_cat is None:
        return _no_data_bundle(raw, "no category has both our and competitor revenue")
    own_rev, comp_rev = by_cat[best_cat]["own"], by_cat[best_cat]["competitor"]
    headline = (f"{best_cat} is our strongest category vs. competitors: "
                f"{format_inr(own_rev)} vs. {format_inr(comp_rev)} combined competitor revenue.")
    visual = _table(["Category", "Side", "Total revenue"],
                     [[r["category"], r["side"], format_inr(r["total_revenue"])] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A revenue comparison by category, not a resourcing recommendation.",
        "firm_up": "Confirm against real sales_data across all tracked brands once connected.",
        "sources": _demo_sources(["sales_data"]),
        "caveat": None,
    }


def present_demo_4(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no bestsellers currently at risk")
    top = min(rows, key=lambda r: r["days_of_stock_remaining"])
    headline = (f"{len(rows)} bestseller(s) at risk -- {top['product_name']} has the least "
                f"runway at {top['days_of_stock_remaining']:.1f} days of stock left.")
    visual = _table(["Product", "Units sold", "Days of stock left"],
                     [[r["product_name"], r["units_sold"], f"{r['days_of_stock_remaining']:.1f}"] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by units sold among low-stock SKUs. Reorder timing is a decision for your team.",
        "firm_up": "Confirm current_stock and reorder lead times against your real inventory system before acting.",
        "sources": _demo_sources(["sales_data", "inventory_data"]),
        "caveat": None,
    }


def present_demo_5(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no recent underperforming launches found")
    top = rows[0]
    headline = (f"{len(rows)} recent launch(es) underperforming -- {top['product_name']} "
                f"sold {top['units_sold']} units vs. a brand average of {top['brand_avg_units_sold']:.1f}.")
    visual = _table(["Product", "Days since launch", "Units sold", "Brand avg units sold"],
                     [[r["product_name"], r["days_since_launch"], r["units_sold"], r["brand_avg_units_sold"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by units sold. Whether to reposition any of these is a decision for your team.",
        "firm_up": "Confirm launch dates against real product-launch records -- this fixture's launch dates are fabricated placeholders.",
        "sources": _demo_sources(["sku_launch_data", "sales_data"]),
        "caveat": "The launch dates behind this list are made up for the demo -- there is no real launch-date record to check them against yet.",
    }


def present_demo_6(raw):
    data = raw["data"]
    margin_rows = data.get("own_margin_by_sku", [])
    price_rows = data.get("avg_price_by_category_brand", [])
    if not margin_rows:
        return _no_data_bundle(raw, "no own-SKU margin data found")
    top = max(margin_rows, key=lambda r: float(r["margin_percent"]))
    headline = f"{top['product_name']} carries our widest margin at {format_pct(top['margin_percent'])}."
    columns = [{"label": "Category", "kind": "real"}, {"label": "Brand", "kind": "real"},
               {"label": "Avg price", "kind": "real"}]
    visual = _table(columns, [[r["category"], r["brand"], format_inr(r["avg_price"])] for r in price_rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Our own margin by SKU, shown next to real competitor pricing -- not a pricing recommendation.",
        "firm_up": "Confirm unit_cost against real supplier/COGS records -- margin_percent here is a synthetic estimate.",
        "sources": _demo_sources(["margin_data"]) + [{"label": "price_history (real)", "url": None, "as_of": None, "kind": "real"}],
        "caveat": "A competitor's true costs and margins are never public -- treat any competitor margin figure here as a demo placeholder, not a real finding.",
    }


def present_demo_7(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no SKUs with both margin and sales data")
    top = rows[0]
    headline = (f"{top['product_name']} has the best margin-to-volume mix: "
                f"{format_pct(top['margin_percent'])} margin across {top['units_sold']} units sold.")
    visual = _table(["Product", "Margin %", "Units sold", "Score"],
                     [[r["product_name"], format_pct(r["margin_percent"]), r["units_sold"], r["margin_volume_score"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by a margin x volume score. Which to prioritize is a decision for your team.",
        "firm_up": "Confirm unit_cost against real supplier/COGS records -- margin_percent here is a synthetic estimate.",
        "sources": _demo_sources(["margin_data", "sales_data"]),
        "caveat": None,
    }


def present_demo_8(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no category stock/sales data found")
    top = rows[0]
    ratio = top.get("stock_to_sales_ratio")
    headline = (f"{top['category']} has the highest stock-to-sales ratio"
                + (f" at {ratio:.1f}x." if ratio is not None else " (no sales recorded)."))
    visual = _table(["Category", "Total stock", "Total units sold", "Stock/sales ratio"],
                     [[r["category"], r["total_stock"], r["total_units_sold"],
                       f"{r['stock_to_sales_ratio']:.1f}" if r["stock_to_sales_ratio"] is not None else "n/a"] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by stock-to-sales ratio. Whether to run a promotion is a decision for your team.",
        "firm_up": "Confirm current_stock and units_sold against real inventory/sales systems before promoting.",
        "sources": _demo_sources(["inventory_data", "sales_data"]),
        "caveat": None,
    }


def present_demo_9(raw):
    rows = raw["data"]
    if len(rows) < 2:
        return _no_data_bundle(raw, "missing margin data for one or both categories")
    by_cat = {r["category"]: r for r in rows}
    cookware, bakeware = by_cat.get("Cookware"), by_cat.get("Bakeware")
    if not cookware or not bakeware:
        return _no_data_bundle(raw, "missing margin data for Cookware or Bakeware")
    diff = float(cookware["avg_margin_percent"]) - float(bakeware["avg_margin_percent"])
    better = "Cookware" if diff > 0 else "Bakeware"
    headline = (f"{better} is more profitable: Cookware averages {format_pct(cookware['avg_margin_percent'])} margin "
                f"vs. Bakeware's {format_pct(bakeware['avg_margin_percent'])}.")
    visual = _table(["Category", "Avg margin %", "SKUs"],
                     [[r["category"], format_pct(r["avg_margin_percent"]), r["n_skus"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A margin comparison between two categories, not a resourcing recommendation.",
        "firm_up": "Confirm unit_cost against real supplier/COGS records -- margin_percent here is a synthetic estimate.",
        "sources": _demo_sources(["margin_data"]),
        "caveat": None,
    }


def present_demo_10(raw):
    data = raw["data"]
    flagged = data.get("flagged_low_conversion_skus", [])
    avg_rate = data.get("brand_avg_conversion_rate_pct")
    if not flagged:
        return _no_data_bundle(raw, "no SKUs flagged for low conversion")
    top = flagged[0]
    headline = (f"{len(flagged)} SKU(s) flagged for low conversion -- {top['product_name']} converts at "
                f"{format_pct(top['conversion_rate_pct'])} vs. a brand average of {format_pct(avg_rate)}.")
    visual = _table(["Product", "Price", "Conversion %"],
                     [[r["product_name"], format_inr(r["price"]), format_pct(r["conversion_rate_pct"])] for r in flagged])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by conversion rate. Whether pricing is the cause is a decision for your team to investigate.",
        "firm_up": "Confirm with real, independently-collected session/traffic data before concluding price is the cause.",
        "sources": _demo_sources(["traffic_data", "channel_performance"]) + [{"label": "price_history (real)", "url": None, "as_of": None, "kind": "real"}],
        "caveat": "This conversion figure is entangled with how the demo generated sales from price/discount, so it may just be echoing that generator rule rather than a real pricing problem.",
    }


def present_demo_11(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no channel revenue recorded")
    top = rows[0]
    headline = f"{top['channel']} drives the most revenue: {format_inr(top['revenue'])} from {top['orders']} orders."
    visual = _bar([r["channel"] for r in rows], [float(r["revenue"]) for r in rows], "₹")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Ranked by revenue as generated in this demo -- not a channel-investment recommendation.",
        "firm_up": "Confirm against your real channel-performance/analytics dashboard once connected.",
        "sources": _demo_sources(["channel_performance"]),
        "caveat": None,
    }


def present_demo_12(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "nothing low on stock on any channel")
    top = rows[0]
    headline = f"{top['product_name']} is down to {top['stock']} unit(s) on {top['channel']}."
    visual = _table(["Product", "Channel", "Stock"], [[r["product_name"], r["channel"], r["stock"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by stock level. Reorder/reallocation is a decision for your team.",
        "firm_up": "Confirm with a real per-channel warehouse allocation system -- this per-channel split is a synthetic estimate.",
        "sources": _demo_sources(["channel_inventory"]),
        "caveat": None,
    }


def present_demo_13(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no quick-commerce channel present")
    # The handler filters to qCommerce channels but does NOT sort by
    # revenue (rows come back in whatever order the SQL grouping returns,
    # i.e. alphabetical by channel) -- rows[0] was a real bug here, caught
    # by test_presenters.py's ranking check: it named "Blinkit" as top
    # while Instamart actually had higher revenue.
    top = max(rows, key=lambda r: float(r["revenue"]))
    headline = f"{top['channel']} is our top quick-commerce channel at {format_inr(top['revenue'])} revenue."
    visual = _bar([r["channel"] for r in rows], [float(r["revenue"]) for r in rows], "₹")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by revenue. Whether to expand assortment there is a decision for your team.",
        "firm_up": "Confirm against your real quick-commerce channel dashboard once connected.",
        "sources": _demo_sources(["channel_performance"]),
        "caveat": None,
    }


def present_demo_14(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no channel return/complaint data found")
    top = rows[0]
    headline = (f"{top['channel']} has our highest return rate at "
                f"{format_pct(top['avg_return_rate'])}, with {top['total_complaints']} complaints logged.")
    visual = _table(["Channel", "Return %", "Complaints", "Top reason"],
                     [[r["channel"], format_pct(r["avg_return_rate"]), r["total_complaints"], r["most_common_complaint_reason"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Channels, ranked by return rate -- complaints are shown separately since they don't always agree.",
        "firm_up": "Confirm against real returns/support-ticket systems once connected.",
        "sources": _demo_sources(["channel_performance", "complaint_data"]),
        "caveat": "Return rate and complaint counts were generated independently, so they can disagree on which channel is worst -- that's by design, not an error.",
    }


def present_demo_15(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no availability data found")
    competitors = [r for r in rows if r["brand_name"] != "Nestasia"]
    if not competitors:
        return _no_data_bundle(raw, "no competitor availability data found")
    worst = max(competitors, key=lambda r: r["unavailable_pct"] or 0)
    headline = f"{worst['brand_name']} has the highest unavailability at {format_pct(worst['unavailable_pct'])} of its SKUs."
    visual = _table(["Brand", "Total SKUs", "Unavailable", "Unavailable %"],
                     [[r["brand_name"], r["total_skus"], r["unavailable_skus"], format_pct(r["unavailable_pct"])] for r in rows],
                     kind="real")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A real availability comparison across brands, not an assortment recommendation.",
        "firm_up": "This is already real, live-collected stock-status data -- no further confirmation needed beyond re-collecting if it's since changed.",
        "sources": [{"label": "sku (real)", "url": None, "as_of": None, "kind": "real"}],
        "caveat": None,
    }


def present_demo_16(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no ad-spend data found")
    top = rows[0]
    product = top.get("product_name") or "an unattributed product"
    headline = f"The ad linked to {product} shows the most attributed orders: {top['attributed_orders']}."
    columns = [{"label": "Theme", "kind": "real"}, {"label": "Product", "kind": "demo"},
               {"label": "Attributed orders", "kind": "demo"}]
    visual = _table(columns, [[r["theme"], r.get("product_name") or "n/a", r["attributed_orders"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Ads, ranked by attributed orders -- not a claim about which campaign truly drove sales.",
        "firm_up": "Confirm with real per-SKU ad attribution -- this ad-to-product link is fabricated for the demo.",
        "sources": [{"label": "paid_ad_creative (real)", "url": None, "as_of": None, "kind": "real"}] + _demo_sources(["ad_spend_data"]),
        "caveat": "Which product each ad actually targets is invented for this demo -- real ad data has no such link at all.",
    }


def present_demo_17(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no ad-spend data with a product link found")
    top = rows[0]
    headline = f"The ad on {top['product_name']} returns the most orders per rupee spent: {top['orders_per_rupee_spend']:.4f}."
    visual = _table(["Product", "Spend", "Orders per ₹ spent"],
                     [[r["product_name"], format_inr(r["spend"]), f"{r['orders_per_rupee_spend']:.4f}" if r["orders_per_rupee_spend"] is not None else "n/a"] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by orders per rupee spent. Whether to increase spend is a decision for your team.",
        "firm_up": "Confirm with real spend and attribution data -- both the spend figure and the product link are fabricated for this demo.",
        "sources": _demo_sources(["ad_spend_data"]),
        "caveat": "Both the spend figures and which product each ad targets are invented for this demo -- there is no real baseline to compare against yet.",
    }


def present_demo_18(raw):
    data = raw["data"]
    avg_disc = data.get("avg_units_sold_discounted_skus")
    avg_full = data.get("avg_units_sold_full_price_skus")
    n_disc, n_full = data.get("n_discounted", 0), data.get("n_full_price", 0)
    if avg_disc is None:
        return _no_data_bundle(raw, "no discounted SKUs found")
    headline = (f"Discounted SKUs ({n_disc}) average {avg_disc:.1f} units sold"
                + (f", vs. {avg_full:.1f} for full-price SKUs ({n_full})." if avg_full is not None else "; no full-price SKUs to compare."))
    columns = [{"label": "Group", "kind": "demo"}, {"label": "Avg units sold", "kind": "demo"}, {"label": "SKUs", "kind": "real"}]
    visual = _table(columns, [
        ["Discounted", f"{avg_disc:.1f}", n_disc],
        ["Full price", f"{avg_full:.1f}" if avg_full is not None else "n/a", n_full],
    ])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A discount-vs-full-price comparison, not proof that discounting works.",
        "firm_up": "This comparison is circular by construction -- confirm with a real campaign period and real sales data instead.",
        "sources": [{"label": "price_history (real)", "url": None, "as_of": None, "kind": "real"}] + _demo_sources(["sales_data"]),
        "caveat": "This demo's sales figures were generated FROM each SKU's discount level, so finding discounted items sell more just confirms that generator rule -- it isn't independent evidence discounting works.",
    }


def present_demo_19(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no social engagement data found")
    top = max(rows, key=lambda r: r["likes"])
    headline = f"{top['platform']} is our best-engaging platform with {top['likes']} likes across {top['n_posts']} posts."
    visual = _table(["Platform", "Likes", "Comments", "Shares", "Posts"],
                     [[r["platform"], r["likes"], r["comments"], r["shares"], r["n_posts"]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Platform-level engagement only -- this cannot say which specific product to promote next.",
        "firm_up": "Confirm against real, product-tagged social analytics -- this data has no product link at all.",
        "sources": _demo_sources(["social_engagement"]),
        "caveat": None,
    }


def present_demo_20(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no competitor ad data found")
    top = rows[0]
    headline = f"{top['brand_name']} is the most active competitor advertiser with {top['n_ads']} ad(s) on the theme '{top['theme']}'."
    visual = _table(["Brand", "Theme", "Ads", "Avg days running"],
                     [[r["brand_name"], r["theme"], r["n_ads"], r["avg_days_running"]] for r in rows],
                     kind="real")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "An activity ranking, not an effectiveness ranking -- there is no sales attribution behind it.",
        "firm_up": "This is already real ad-library data -- effectiveness would need real per-ad sales attribution, which doesn't exist for anyone here.",
        "sources": [{"label": "paid_ad_creative (real)", "url": None, "as_of": None, "kind": "real"}],
        "caveat": "Most ads here, not most effective ads -- there's no way to measure which ad actually drove a sale, for us or for competitors.",
    }


def present_demo_21(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no SKUs with complete sales/margin/inventory data")
    top = rows[0]
    headline = f"{top['product_name']} scores highest on the combined priority score ({top['priority_score']:.1f})."
    visual = _table(["Product", "Priority score", "Units sold", "Margin %", "Days of stock left"],
                     [[r["product_name"], r["priority_score"], r["units_sold"], format_pct(r["margin_percent"]),
                       f"{r['days_of_stock_remaining']:.1f}"] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by a composite priority score. Which single product to prioritize is a decision for your team.",
        "firm_up": "The marketing component of this score rests on a fabricated ad-to-product link -- confirm with real per-SKU ad attribution before acting on that part.",
        "sources": _demo_sources(["sales_data", "margin_data", "inventory_data", "ad_spend_data"]),
        "caveat": "The marketing part of this score is built on a fabricated ad-to-product link -- treat it as illustrative, not a recovered fact.",
    }


def present_demo_22(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no category data found")
    top = max(rows, key=lambda r: r["total_stock"])
    headline = f"{top['category']} carries the most stock ({top['total_stock']} units) relative to {top['total_units_sold']} sold."
    visual = _table(["Category", "Total stock", "Total units sold", "Avg margin %"],
                     [[r["category"], r["total_stock"], r["total_units_sold"],
                       format_pct(r["avg_margin_percent"]) if r["avg_margin_percent"] is not None else "n/a"] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by stock. Which one thing to fix is a decision for your team.",
        "firm_up": "There is no category literally named 'Kitchen' -- this breaks out all 5 tracked categories instead.",
        "sources": _demo_sources(["sales_data", "inventory_data", "margin_data"]),
        "caveat": None,
    }


def present_demo_23(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no declining-trend SKUs found")
    top = rows[0]
    periods = top["units_sold_by_period"]
    headline = f"{top['product_name']} is trending down: {periods[0]} units in its first period vs. {periods[-1]} in its latest."
    visual = _table(["Product", "First period units", "Latest period units"],
                     [[r["product_name"], r["units_sold_by_period"][0], r["units_sold_by_period"][-1]] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by first-appearance order. Whether to intervene is a decision for your team.",
        "firm_up": "This demo has only random month-to-month noise behind every SKU -- confirm against real, longer-run sales history before treating this as a real signal.",
        "sources": _demo_sources(["sales_data"]),
        "caveat": "Every SKU shares the same tiny global trend with only random monthly noise on top -- a SKU flagged here got unlucky draws, not a real decline.",
    }


def present_demo_24(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no category is more vulnerable than us")
    top = rows[0]
    headline = (f"{top['category']} is our most vulnerable category -- {top['top_competitor']} out-earns us "
                f"{format_inr(top['top_competitor_revenue'])} to {format_inr(top['own_revenue'])}.")
    visual = _table(["Category", "Our revenue", "Top competitor", "Their revenue"],
                     [[r["category"], format_inr(r["own_revenue"]), r["top_competitor"], format_inr(r["top_competitor_revenue"])] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Categories, ranked by the revenue gap. Where to respond is a decision for your team.",
        "firm_up": "Confirm against real sales_data across all tracked brands once connected.",
        "sources": _demo_sources(["sales_data"]),
        "caveat": None,
    }


def present_demo_25(raw):
    rows = raw["data"]
    if not rows:
        return _no_data_bundle(raw, "no cut candidates found")
    top = rows[0]
    headline = f"{top['product_name']} scores lowest on units sold x margin ({top['cut_candidate_score']:.1f})."
    visual = _table(["Product", "Cut-candidate score", "Units sold", "Margin %"],
                     [[r["product_name"], r["cut_candidate_score"], r["units_sold"], format_pct(r["margin_percent"])] for r in rows])
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "Candidates, ranked by a low units-sold x margin score. Which SKUs to actually cut is a decision for your team.",
        "firm_up": "Confirm against real sales_data and margin_data before cutting anything -- these figures are synthetic placeholders.",
        "sources": _demo_sources(["sales_data", "margin_data"]),
        "caveat": None,
    }


# ---------------------------------------------------------------------------
# Live presenter(s) -- call retrieve.retrieve() directly, bypassing
# generate()'s Ollama path entirely.
# ---------------------------------------------------------------------------
def _no_evidence_bundle(reason):
    return {
        "headline": reason,
        "visual": None,
        "suggestion": "N/A",
        "firm_up": "Ensure the requested brand/category has collected data.",
        "sources": [],
        "caveat": None,
    }


def present_live_price_comparison(typed_text):
    result = retrieve(typed_text, db_mode="production")
    own = result["evidence"]["own_brand"]
    comp = result["evidence"]["tracked_competitors"]
    if not own or not comp:
        return _no_evidence_bundle("Not enough matching data to compare pricing here.")
    own_row, comp_row = own[0], comp[0]
    diff_pct = (float(own_row["avg_price"]) - float(comp_row["avg_price"])) / float(comp_row["avg_price"]) * 100
    direction = "higher than" if diff_pct > 0 else "lower than"
    headline = (f"{own_row['brand_name']}'s {own_row['category_name']} averages "
                f"{format_inr(own_row['avg_price'])}, {format_pct(abs(diff_pct))} {direction} "
                f"{comp_row['brand_name']}'s {format_inr(comp_row['avg_price'])}.")
    visual = _table(["Brand", "Category", "Avg price", "SKUs priced"], [
        [own_row["brand_name"], own_row["category_name"], format_inr(own_row["avg_price"]), own_row["priced_skus"]],
        [comp_row["brand_name"], comp_row["category_name"], format_inr(comp_row["avg_price"]), comp_row["priced_skus"]],
    ], kind="real")

    incomplete_sides = [r for r in (own_row, comp_row) if not r.get("all_complete", True)]
    caveat = None
    if incomplete_sides:
        names = " and ".join(f"{r['brand_name']}'s {r['category_name']}" for r in incomplete_sides)
        caveat = (f"{names} collection is confirmed incomplete (fewer SKUs on file than the site "
                  f"actually lists) -- this average is real but not the whole category.")

    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A price comparison, not a pricing recommendation -- any change is a decision for your team.",
        "firm_up": ("This is already real, live-collected pricing data, but re-collect the incomplete "
                    "side(s) before treating this average as final." if incomplete_sides else
                    "This is already real, live-collected pricing data -- no further confirmation needed beyond re-collecting if prices have since changed."),
        "sources": [
            {"label": f"{own_row['brand_name']} price data", "url": None,
             "as_of": format_date(own_row.get("latest_collected_at")), "kind": "real"},
            {"label": f"{comp_row['brand_name']} price data", "url": None,
             "as_of": format_date(comp_row.get("latest_collected_at")), "kind": "real"},
        ],
        "caveat": caveat,
    }


def _mismatch_headline(brand_name, cls):
    if cls is None:
        return f"No stock-display data found for {brand_name}."
    if not cls["brand_testable_for_mismatch"]:
        return f"{brand_name}'s stock-display bug CANNOT BE TESTED with current data -- no SKU has a definitive true/false reading."
    tested = cls["skus_with_confirmed_mismatch"] + cls["skus_confirmed_clean"]
    if cls["brand_has_confirmed_mismatch"]:
        return f"{brand_name} has {cls['skus_with_confirmed_mismatch']} confirmed stock-display mismatch(es) out of {tested} tested SKU(s)."
    return f"{brand_name} shows 0 confirmed stock-display mismatches across {tested} tested SKU(s)."


def _mismatch_bundle(typed_text, subject_brand, side):
    result = retrieve(typed_text, db_mode="production")
    cls = result["evidence"][side]["classification"].get(subject_brand)
    headline = _mismatch_headline(subject_brand, cls)
    if cls is None:
        return _no_evidence_bundle(headline)
    visual = _table(
        ["Metric", "Value"],
        [["Confirmed mismatches", cls["skus_with_confirmed_mismatch"]],
         ["Confirmed clean", cls["skus_confirmed_clean"]],
         ["Not applicable (N/A)", cls["skus_not_applicable_na"]],
         ["Ambiguous/Unknown", cls["skus_ambiguous_unknown"]]],
        kind="real",
    )
    caveats = []
    if not cls["brand_testable_for_mismatch"]:
        caveats.append(f"{subject_brand} cannot be tested for this bug at all -- that is not the same as being clean.")
    if not cls["all_collection_complete"]:
        caveats.append(f"{subject_brand}'s own product collection is confirmed incomplete -- this result reflects "
                        f"only what's been collected so far, not a guaranteed reading across every product.")

    if cls["brand_testable_for_mismatch"]:
        firm_up = ("This is already real, live-collected data, but re-collect the rest of this brand's catalogue "
                    "before treating this result as final." if not cls["all_collection_complete"] else
                    "This is already real, live-collected data -- no further confirmation needed beyond re-testing "
                    "if the site has since changed.")
    else:
        firm_up = "This brand's site architecture gives no on-page signal to test at all -- it cannot be firmed up without a different collection method."

    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A factual test result, not a recommendation.",
        "firm_up": firm_up,
        "sources": [{"label": f"{subject_brand} stock-display data", "url": None,
                     "as_of": format_date(cls.get("latest_collected_at")), "kind": "real"}],
        "caveat": " ".join(caveats) if caveats else None,
    }


def present_live_2(typed_text):
    return _mismatch_bundle(typed_text, "Nestasia", "own_brand")


def present_live_3(typed_text):
    return _mismatch_bundle(typed_text, "Milton", "tracked_competitors")


def present_live_4(typed_text):
    return _mismatch_bundle(typed_text, "Home Centre", "tracked_competitors")


def present_live_5(typed_text):
    return _mismatch_bundle(typed_text, "Borosil", "tracked_competitors")


def present_live_6(typed_text):
    result = retrieve(typed_text, db_mode="production")
    comp_cls = result["evidence"]["tracked_competitors"]["classification"]
    testable = {b: c for b, c in comp_cls.items() if c["brand_testable_for_mismatch"]}
    untestable = {b: c for b, c in comp_cls.items() if not c["brand_testable_for_mismatch"]}
    with_bug = {b: c for b, c in testable.items() if c["brand_has_confirmed_mismatch"]}
    incomplete_testable = {b: c for b, c in testable.items() if not c["all_collection_complete"]}
    headline = (f"{len(with_bug)} of {len(testable)} testable tracked competitor(s) show a confirmed "
                f"stock-display mismatch; {len(untestable)} cannot be tested.")
    visual = _table(
        ["Brand", "Testable", "Confirmed mismatch", "Collection complete"],
        [[b, "Yes" if c["brand_testable_for_mismatch"] else "No",
          ("Yes" if c["brand_has_confirmed_mismatch"] else "No") if c["brand_testable_for_mismatch"] else "n/a",
          "Yes" if c["all_collection_complete"] else "No"]
         for b, c in comp_cls.items()],
        kind="real",
    )
    caveats = []
    if untestable:
        caveats.append(f"{len(untestable)} competitor(s) cannot be tested at all -- they are reported separately, "
                        f"never folded into either the 'has bug' or 'clean' count.")
    if incomplete_testable:
        names = ", ".join(sorted(incomplete_testable))
        caveats.append(f"{names}'s tracked collection is confirmed incomplete -- its result here reflects only "
                        f"what's been collected so far, not a guaranteed reading across its whole catalogue.")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A factual tally across tracked competitors, not a recommendation.",
        "firm_up": ("This is already real, live-collected data across all tracked competitors, but re-collect the "
                    "incomplete brand(s)' catalogues before treating their result as final." if incomplete_testable else
                    "This is already real, live-collected data across all tracked competitors -- no further confirmation needed beyond re-testing."),
        "sources": [{"label": f"{b} stock-display data", "url": None, "as_of": None, "kind": "real"} for b in comp_cls],
        "caveat": " ".join(caveats) if caveats else None,
    }


def present_live_7(typed_text):
    result = retrieve(typed_text, db_mode="production")
    own = result["evidence"]["own_brand"]
    row = next((r for r in own if r.get("category_name") == "Container"), own[0] if own else None)
    if row is None:
        return _no_evidence_bundle("No Container-category data found for Nestasia.")
    headline = f"Nestasia's Container category is {row['status']} -- {row['sku_count']} SKU(s) collected so far."
    visual = _table(["Category", "SKUs collected", "Status"], [[row["category_name"], row["sku_count"], row["status"]]], kind="real")
    return {
        "headline": headline,
        "visual": visual,
        "suggestion": "A collection-completeness fact, not a recommendation.",
        "firm_up": "This is already real, live-collected data -- completeness will update as more SKUs are collected.",
        "sources": [{"label": "Container collection status", "url": None,
                     "as_of": format_date(row.get("latest_collected_at")), "kind": "real"}],
        "caveat": "PARTIAL means this is not the full picture yet -- more SKUs in this category haven't been collected." if row["status"] == "PARTIAL" else None,
    }


def present_live_8(typed_text):
    result = retrieve(typed_text, db_mode="production")
    return {
        "headline": result["note"],
        "visual": None,
        "suggestion": "N/A -- this question cannot be answered from this schema at all.",
        "firm_up": "Would require adding a real sales/revenue table to the live schema.",
        "sources": [],
        "caveat": None,
    }


PRESENTERS = {
    "demo_1": present_demo_1, "demo_2": present_demo_2, "demo_3": present_demo_3,
    "demo_4": present_demo_4, "demo_5": present_demo_5, "demo_6": present_demo_6,
    "demo_7": present_demo_7, "demo_8": present_demo_8, "demo_9": present_demo_9,
    "demo_10": present_demo_10, "demo_11": present_demo_11, "demo_12": present_demo_12,
    "demo_13": present_demo_13, "demo_14": present_demo_14, "demo_15": present_demo_15,
    "demo_16": present_demo_16, "demo_17": present_demo_17, "demo_18": present_demo_18,
    "demo_19": present_demo_19, "demo_20": present_demo_20, "demo_21": present_demo_21,
    "demo_22": present_demo_22, "demo_23": present_demo_23, "demo_24": present_demo_24,
    "demo_25": present_demo_25,
    "live_1": present_live_price_comparison, "live_2": present_live_2, "live_3": present_live_3,
    "live_4": present_live_4, "live_5": present_live_5, "live_6": present_live_6,
    "live_7": present_live_7, "live_8": present_live_8,
}
