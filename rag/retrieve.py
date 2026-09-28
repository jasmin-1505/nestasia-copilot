"""
Structured retrieval layer for the Nestasia competitive-intelligence
database. Given a natural-language question, classifies it into one of a
small set of supported intents and runs a real SQL query against
PRODUCTION -- no vector search, no embeddings, because the data this
project actually has is structured (SKU/price/stock/category rows), not
prose. review_set has 0 rows as of this build (the JTBD theme-extraction
batch hasn't run), so no review-based retrieval is implemented yet --
building vector search against an empty table would be premature, not
forward-looking.

Every intent's SQL result comes back WITH its source metadata attached
(source_type, collected_at, collection_complete) on every row/group, not
just the requested numbers -- generate.py's citation discipline depends on
this being present at retrieval time, not reconstructed later.

Supported intents (minimum set from the brief):
  - price_comparison        : avg/min/max price by brand x category
  - stock_mismatch_lookup   : per-brand mismatch breakdown + example SKUs
  - stock_mismatch_aggregate: the same breakdown across ALL tracked brands,
                              with an explicit testable/untestable/has_bug
                              classification computed HERE in code, not
                              left for the language model to infer from raw
                              counts -- this is the direct answer to the
                              brief's warning about "averaging N/A or
                              Unknown into a false sense of clean."
  - sku_count_by_category   : SKU counts by brand x category
  - completeness_check      : which brand x category groups are partial
  - ad_theme_lookup         : paid_ad_creative rows/themes by brand
  - unsupported_internal_data: question asks for data this schema has no
                              table for at all (sales, margin, etc.) --
                              returns zero evidence rows and an explicit
                              reason, so generate.py has something concrete
                              to refuse with rather than an ambiguous empty
                              result.

Usage (library):
    from retrieve import retrieve
    result = retrieve("How does our Cookware pricing compare to Home Centre's?")
    # result["intent"], result["evidence"] (list of dict rows with source
    # metadata attached), result["note"] (human-readable summary, always
    # empty-safe)

Usage (CLI, for manual testing):
    python retrieve.py "How does our Cookware pricing compare to Home Centre's?"
"""

import json
import os
import re
import sys
from collections import defaultdict

import psycopg2
import psycopg2.extras

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")

KNOWN_BRANDS = {
    "nestasia": "Nestasia",
    "wonderchef": "Wonderchef",
    "home centre": "Home Centre",
    "home center": "Home Centre",
    "milton": "Milton",
    "prestige": "Prestige",
    "borosil": "Borosil",
}

KNOWN_CATEGORIES = {
    "cookware": "Cookware",
    "bakeware": "Bakeware",
    "container": "Container",
    "lunch box": "Lunch Boxes+Bags",
    "lunch bag": "Lunch Boxes+Bags",
    "kitchen rack": "Kitchen Racks+Trivets",
    "trivet": "Kitchen Racks+Trivets",
}

INTERNAL_DATA_TERMS = (
    "best-selling", "best selling", "bestseller", "sales", "units sold",
    "revenue", "margin", "profit", "conversion rate", "cart abandonment",
)


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


def _connect(prefix):
    """No default prefix -- every call site must say PRODUCTION or FIXTURE
    explicitly. An earlier version defaulted to "PRODUCTION", which is
    exactly the kind of silent inference the mode switch below exists to
    rule out; removed rather than left as a trap for a future call site
    that forgets to pass one."""
    if prefix not in ("PRODUCTION", "FIXTURE"):
        raise ValueError(f"_connect() requires prefix='PRODUCTION' or 'FIXTURE' explicitly, got {prefix!r}")
    env = _load_env()
    return psycopg2.connect(
        host=env[f"{prefix}_DB_HOST"], port=env[f"{prefix}_DB_PORT"],
        dbname=env[f"{prefix}_DB_NAME"], user=env[f"{prefix}_DB_USER"],
        password=env[f"{prefix}_DB_PASSWORD"], sslmode="require", connect_timeout=15,
    )


# ---------------------------------------------------------------------------
# Intent classification -- keyword/pattern based, not an LLM call. This is a
# small, fixed set of structured query shapes; a rule-based router is more
# predictable and auditable than asking a model to emit SQL directly, and
# doesn't depend on Ollama being available at all.
# ---------------------------------------------------------------------------
def classify_intent(question):
    q = question.lower()

    mentioned_brands = [canon for key, canon in KNOWN_BRANDS.items() if key in q]
    if re.search(r"\bour\b|\bwe\b|\bus\b", q) and "Nestasia" not in mentioned_brands:
        mentioned_brands.append("Nestasia")
    mentioned_categories = [canon for key, canon in KNOWN_CATEGORIES.items() if key in q]

    if any(term in q for term in INTERNAL_DATA_TERMS):
        return {"intent": "unsupported_internal_data", "question": question}

    is_aggregate_bug_question = (
        ("how many" in q or "how much" in q) and
        ("competitor" in q or "brand" in q or "of our tracked" in q) and
        any(t in q for t in ("bug", "mismatch", "inconsist", "stock-display", "stock display"))
    )
    if is_aggregate_bug_question:
        return {"intent": "stock_mismatch_aggregate", "question": question}

    if any(t in q for t in ("stock-display", "stock display", "mismatch", "inconsist")) or \
       ("bug" in q and ("stock" in q or "we do" in q or "this" in q)):
        return {
            "intent": "stock_mismatch_lookup",
            "brands": mentioned_brands or ["Nestasia"],
            "question": question,
        }

    if any(t in q for t in ("incomplete", "partial", "collection_complete")) or \
       ("complete" in q and "data" in q):
        return {
            "intent": "completeness_check",
            "brands": mentioned_brands,
            "categories": mentioned_categories,
            "question": question,
        }

    if any(t in q for t in ("ad ", " ads", "advertisement", "ad library", "ad creative", "ad theme")):
        return {"intent": "ad_theme_lookup", "brands": mentioned_brands, "question": question}

    if any(t in q for t in ("price", "pricing", "cost", "cheaper", "expensive")):
        return {
            "intent": "price_comparison",
            "brands": mentioned_brands,
            "categories": mentioned_categories,
            "question": question,
        }

    if any(t in q for t in ("how many skus", "sku count", "number of products", "count of products")):
        return {
            "intent": "sku_count_by_category",
            "brands": mentioned_brands,
            "categories": mentioned_categories,
            "question": question,
        }

    return {"intent": "unknown", "question": question}


# ---------------------------------------------------------------------------
# Shared brand/category filter helpers
# ---------------------------------------------------------------------------
_BRAND_NAME_EXPR = "COALESCE(b.name, cb.name)"


def _brand_join_sql():
    return "LEFT JOIN brand b ON b.id = s.brand_id LEFT JOIN competitor_brand cb ON cb.id = s.competitor_brand_id"


def _brand_filter(brands):
    if not brands:
        return "TRUE", []
    return f"{_BRAND_NAME_EXPR} = ANY(%s)", [brands]


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------
def _price_comparison(cur, brands, categories):
    where, params = _brand_filter(brands)
    cat_where = "c.name = ANY(%s)" if categories else "TRUE"
    if categories:
        params = params + [categories]

    cur.execute(
        f"""
        SELECT {_BRAND_NAME_EXPR} AS brand_name, c.name AS category_name,
               COUNT(*) AS total_skus,
               COUNT(ph.price) AS priced_skus,
               ROUND(AVG(ph.price), 2) AS avg_price,
               MIN(ph.price) AS min_price,
               MAX(ph.price) AS max_price,
               bool_and(s.collection_complete) AS all_complete,
               array_agg(DISTINCT sr.source_type::text) AS source_types,
               MAX(sr.collected_at) AS latest_collected_at
        FROM sku s
        {_brand_join_sql()}
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        LEFT JOIN price_history ph ON ph.sku_id = s.id
        JOIN source_record sr ON sr.id = s.source_record_id
        WHERE {where} AND {cat_where}
        GROUP BY brand_name, category_name
        ORDER BY brand_name, category_name
        """,
        params,
    )
    rows = cur.fetchall()
    evidence = [dict(r) for r in rows]
    return evidence


def _stock_mismatch_breakdown(cur, brands):
    """Returns per-brand mismatch counts AND an explicit
    testable/untestable/has_bug classification computed here, not left to
    the language model.

    Field names below were deliberately renamed from an earlier version
    (true_count/false_count/testable/confirmed_clean) after a real test run
    showed a local 8B model misreading them -- "false_count" in particular
    reads, on a fast pass, like "count of bad/false things" rather than its
    actual meaning ("count of SKUs where stock_mismatch=false, i.e.
    correctly agreeing, no bug"). The boolean brand-level flags are also
    prefixed `brand_` specifically so they can't collide in meaning with
    the per-SKU counts (`skus_confirmed_clean` is a COUNT; the old,
    unprefixed `confirmed_clean` name was easy to conflate with it). This
    is a real improvement in legibility, not a full fix on its own -- see
    NOTES.md's write-up of this round's test for whether it actually moved
    the model's error rate.
    """
    where, params = _brand_filter(brands)
    cur.execute(
        f"""
        SELECT {_BRAND_NAME_EXPR} AS brand_name, s.stock_mismatch::text AS mismatch_value,
               COUNT(*) AS n, bool_and(s.collection_complete) AS all_complete,
               array_agg(DISTINCT sr.source_type::text) AS source_types,
               MAX(sr.collected_at) AS latest_collected_at
        FROM sku s
        {_brand_join_sql()}
        JOIN source_record sr ON sr.id = s.source_record_id
        WHERE {where}
        GROUP BY brand_name, mismatch_value
        ORDER BY brand_name, mismatch_value
        """,
        params,
    )
    raw = [dict(r) for r in cur.fetchall()]

    by_brand = defaultdict(lambda: {"true": 0, "false": 0, "N/A": 0, "Unknown": 0,
                                     "all_complete": True, "source_types": set(), "latest_collected_at": None})
    for r in raw:
        b = by_brand[r["brand_name"]]
        b[r["mismatch_value"]] = r["n"]
        b["all_complete"] = b["all_complete"] and bool(r["all_complete"])
        b["source_types"] |= set(r["source_types"])
        if r["latest_collected_at"] and (b["latest_collected_at"] is None or r["latest_collected_at"] > b["latest_collected_at"]):
            b["latest_collected_at"] = r["latest_collected_at"]

    classification = {}
    for brand_name, counts in by_brand.items():
        tested_count = counts["true"] + counts["false"]
        classification[brand_name] = {
            # Renamed from true_count / false_count / na_count / unknown_count:
            "skus_with_confirmed_mismatch": counts["true"],
            "skus_confirmed_clean": counts["false"],
            "skus_not_applicable_na": counts["N/A"],
            "skus_ambiguous_unknown": counts["Unknown"],
            # Renamed from testable / has_confirmed_bug / confirmed_clean,
            # prefixed brand_ to avoid colliding in meaning with the
            # per-SKU counts above:
            "brand_testable_for_mismatch": tested_count > 0,
            "brand_has_confirmed_mismatch": counts["true"] > 0,
            "brand_confirmed_clean": tested_count > 0 and counts["true"] == 0,
            "all_collection_complete": counts["all_complete"],
            "source_types": sorted(counts["source_types"]),
            "latest_collected_at": counts["latest_collected_at"].isoformat() if counts["latest_collected_at"] else None,
        }
    return classification


def _stock_mismatch_lookup(cur, brands):
    classification = _stock_mismatch_breakdown(cur, brands)

    where, params = _brand_filter(brands)
    cur.execute(
        f"""
        SELECT {_BRAND_NAME_EXPR} AS brand_name, s.product_name, s.product_url,
               s.stock_status_tag, s.actual_button_state, s.collection_complete,
               sr.source_type::text AS source_type, sr.collected_at
        FROM sku s
        {_brand_join_sql()}
        JOIN source_record sr ON sr.id = s.source_record_id
        WHERE {where} AND s.stock_mismatch = 'true'
        ORDER BY brand_name, s.product_name
        LIMIT 30
        """,
        params,
    )
    example_rows = [dict(r) for r in cur.fetchall()]
    return {"classification": classification, "example_mismatched_skus": example_rows}


def _stock_mismatch_aggregate(cur):
    # No brand filter -- every tracked brand, own + competitors.
    return {"classification": _stock_mismatch_breakdown(cur, [])}


def _sku_count_by_category(cur, brands, categories):
    where, params = _brand_filter(brands)
    cat_where = "c.name = ANY(%s)" if categories else "TRUE"
    if categories:
        params = params + [categories]
    cur.execute(
        f"""
        SELECT {_BRAND_NAME_EXPR} AS brand_name, c.name AS category_name,
               COUNT(*) AS sku_count, bool_and(s.collection_complete) AS all_complete,
               array_agg(DISTINCT sr.source_type::text) AS source_types,
               MAX(sr.collected_at) AS latest_collected_at
        FROM sku s
        {_brand_join_sql()}
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        JOIN source_record sr ON sr.id = s.source_record_id
        WHERE {where} AND {cat_where}
        GROUP BY brand_name, category_name
        ORDER BY brand_name, category_name
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def _completeness_check(cur, brands, categories):
    where, params = _brand_filter(brands)
    cat_where = "c.name = ANY(%s)" if categories else "TRUE"
    if categories:
        params = params + [categories]
    cur.execute(
        f"""
        SELECT {_BRAND_NAME_EXPR} AS brand_name, c.name AS category_name,
               COUNT(*) AS sku_count, bool_and(s.collection_complete) AS all_complete,
               bool_or(NOT s.collection_complete) AS any_partial,
               array_agg(DISTINCT sr.source_type::text) AS source_types,
               MAX(sr.collected_at) AS latest_collected_at
        FROM sku s
        {_brand_join_sql()}
        JOIN sku_category sc ON sc.sku_id = s.id
        JOIN category c ON c.id = sc.category_id
        JOIN source_record sr ON sr.id = s.source_record_id
        WHERE {where} AND {cat_where}
        GROUP BY brand_name, category_name
        ORDER BY brand_name, category_name
        """,
        params,
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        r["status"] = "PARTIAL" if r["any_partial"] else "complete"
    return rows


def _ad_theme_lookup(cur, brands):
    where = f"{_BRAND_NAME_EXPR} = ANY(%s)" if brands else "TRUE"
    params = [brands] if brands else []
    cur.execute(
        f"""
        SELECT COALESCE(b.name, cb.name) AS brand_name, pac.theme, pac.hook_headline,
               pac.offer_discount, pac.ad_format, pac.days_running, pac.status,
               sr.source_type::text AS source_type, sr.collected_at
        FROM paid_ad_creative pac
        LEFT JOIN brand b ON b.id = pac.brand_id
        LEFT JOIN competitor_brand cb ON cb.id = pac.competitor_brand_id
        JOIN source_record sr ON sr.id = pac.source_record_id
        WHERE {where}
        ORDER BY brand_name, pac.theme NULLS LAST
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Own-brand vs. tracked-competitor split -- structural, not left for the
# model to infer from context. A real test run had the model list
# "Nestasia" inside a "confirmed clean competitors" bucket it built itself
# from a flat brand list -- Nestasia is OUR brand, not a tracked
# competitor, and that distinction was nowhere in the evidence's shape,
# only inferable from the name itself. Every intent's evidence is now
# wrapped in {"own_brand": ..., "tracked_competitors": ...} at the top
# level so the model never has to make that call itself.
# ---------------------------------------------------------------------------
_OWN_BRAND_NAME = "Nestasia"


def _split_evidence_by_ownership(evidence):
    def split_rows(rows):
        own = [r for r in rows if r.get("brand_name") == _OWN_BRAND_NAME]
        comp = [r for r in rows if r.get("brand_name") != _OWN_BRAND_NAME]
        return own, comp

    def split_dict_by_brand_key(d):
        own = {k: v for k, v in d.items() if k == _OWN_BRAND_NAME}
        comp = {k: v for k, v in d.items() if k != _OWN_BRAND_NAME}
        return own, comp

    if isinstance(evidence, list):
        own, comp = split_rows(evidence)
        return {"own_brand": own, "tracked_competitors": comp}

    if isinstance(evidence, dict) and "classification" in evidence:
        own_cls, comp_cls = split_dict_by_brand_key(evidence["classification"])
        result = {
            "own_brand": {"classification": own_cls},
            "tracked_competitors": {"classification": comp_cls},
        }
        if "example_mismatched_skus" in evidence:
            own_ex, comp_ex = split_rows(evidence["example_mismatched_skus"])
            result["own_brand"]["example_mismatched_skus"] = own_ex
            result["tracked_competitors"]["example_mismatched_skus"] = comp_ex
        return result

    # unsupported_internal_data / unknown -- already empty, nothing to split.
    return evidence


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
DB_MODES = ("production", "fixture")


def retrieve_fixture_question(question_number):
    """Deterministic routing for the 25 fixed gold-set questions -- imported
    lazily to avoid a circular import (fixture_business_queries imports
    _connect from this module)."""
    from fixture_business_queries import run_fixture_question
    return run_fixture_question(question_number)


def retrieve(question, db_mode, question_number=None):
    """db_mode is REQUIRED, not defaulted -- 'production' (real data) or
    'fixture' (synthetic demo data). Never inferred from the question text:
    a question that happens to look like one of the 25 business-question
    gold-set items is not, by itself, evidence the caller wants fixture
    data -- the caller must say so explicitly every time. This is a hard
    requirement, not a convenience default, because the two databases carry
    fundamentally different trust levels and mixing them up silently is
    exactly the failure this switch exists to prevent.

    question_number (1-25, optional): when given, routes deterministically
    to the exact gold-set intent for that question via
    FIXTURE_GOLD_SET_ROUTES (see that section below) instead of the
    keyword-based classify_intent() -- the 25 gold-set questions are a
    FIXED, KNOWN set, not open natural language, so exact routing by number
    is more reliable than fuzzy re-classification of their text every time.
    """
    if db_mode not in DB_MODES:
        raise ValueError(f"retrieve() requires db_mode='production' or 'fixture' explicitly, got {db_mode!r} "
                          f"-- this is never inferred or defaulted.")

    if db_mode == "fixture" and question_number is not None:
        return retrieve_fixture_question(question_number)

    parsed = classify_intent(question)
    intent = parsed["intent"]

    if intent == "unsupported_internal_data":
        return {
            "intent": intent,
            "question": question,
            "db_mode": db_mode,
            "evidence": [],
            "note": ("This question asks about sales, margin, or other internal business "
                     "data. This database only contains publicly-collected storefront/ad data "
                     "(SKU, price, stock-display, and ad-creative observations) -- it has no "
                     "table for sales, revenue, or margin at all."),
        }

    if intent == "unknown":
        return {
            "intent": intent,
            "question": question,
            "db_mode": db_mode,
            "evidence": [],
            "note": "Could not classify this question into a supported retrieval intent.",
        }

    conn = _connect(db_mode.upper())
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if intent == "price_comparison":
                evidence = _price_comparison(cur, parsed.get("brands", []), parsed.get("categories", []))
            elif intent == "stock_mismatch_lookup":
                evidence = _stock_mismatch_lookup(cur, parsed.get("brands", []))
            elif intent == "stock_mismatch_aggregate":
                evidence = _stock_mismatch_aggregate(cur)
            elif intent == "sku_count_by_category":
                evidence = _sku_count_by_category(cur, parsed.get("brands", []), parsed.get("categories", []))
            elif intent == "completeness_check":
                evidence = _completeness_check(cur, parsed.get("brands", []), parsed.get("categories", []))
            elif intent == "ad_theme_lookup":
                evidence = _ad_theme_lookup(cur, parsed.get("brands", []))
            else:
                evidence = []
    finally:
        conn.close()

    evidence = _split_evidence_by_ownership(evidence)
    return {"intent": intent, "question": question, "db_mode": db_mode, "params": parsed, "evidence": evidence, "note": ""}


def _json_default(o):
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


if __name__ == "__main__":
    q = " ".join(sys.argv[1:]) or "How does our Cookware pricing compare to Home Centre's?"
    result = retrieve(q, db_mode="production")
    print(json.dumps(result, indent=2, default=_json_default))
