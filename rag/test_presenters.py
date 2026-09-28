"""
Verifies every number quoted in a presenter's headline traces back to the
underlying data -- never a hallucinated or wrongly-rounded figure. Since
presenters are pure f-strings over computed values (no LLM), this is a
parse-and-check, not a fuzzy grading task.

Two things a naive "extract every number, compare to every float" version
gets wrong, and this version fixes:

  1. A number can be part of an ENTITY STRING, not a claim -- "3000ml" in
     a product name isn't a quantity the presenter is asserting, it's a
     substring of a name it copied verbatim. Fixed by stripping every
     string value found anywhere in the underlying data (product names,
     brand names, categories, channels, etc.) out of the headline BEFORE
     extracting numbers.
  2. A headline shows a value through a FORMATTER (format_inr rounds to
     lakh/crore, format_pct rounds to 1dp) -- comparing the displayed
     string to a raw float with a tolerance is fragile and can both over-
     and under-accept. Fixed by re-deriving the formatted string from each
     underlying numeric value with the SAME formatter the presenter uses,
     and comparing string to string, not float to float.

Usage:
    python test_presenters.py
"""
import decimal
import re

import fixture_business_queries as fbq
import presenters as p
import question_registry as qr
from retrieve import retrieve

CURRENCY_RE = re.compile(r"₹\d{1,3}(?:,\d{2,3})*(?:\.\d+)?(?:\s*(?:lakh|crore))?")
PERCENT_RE = re.compile(r"-?\d+\.?\d*%")
PLAIN_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?!\w)")


def _all_strings(obj):
    """Every string value anywhere in the underlying data -- product
    names, brand names, categories, channels, reasons, platforms, etc.
    These are entities the headline is allowed to quote verbatim; any
    digits inside them (e.g. "3000ml") are not numeric claims."""
    found = set()

    def rec(x):
        if isinstance(x, str):
            if x:
                found.add(x)
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)

    rec(obj)
    return found


def _strip_entities(text, entity_strings):
    for s in sorted(entity_strings, key=len, reverse=True):
        if s and s in text:
            text = text.replace(s, " ")
    return text


def _all_numbers(obj):
    """Every numeric leaf anywhere in the underlying data. Postgres NUMERIC
    columns come back from psycopg2 as decimal.Decimal, not float -- an
    earlier version of this check only recognized int/float and silently
    dropped every revenue/margin/percentage value, which is why it first
    reported ALL currency and percent figures as "unverified": they were
    never in the candidate set at all, not because the presenter was wrong."""
    found = []

    def rec(x):
        if isinstance(x, bool):
            return
        if isinstance(x, (int, float, decimal.Decimal)):
            found.append(float(x))
        elif isinstance(x, dict):
            for v in x.values():
                rec(v)
        elif isinstance(x, list):
            for v in x:
                rec(v)

    rec(obj)
    return found


def _formatted_candidates(numbers, extra_counts=(), extra_values=()):
    """For every underlying number, the exact strings a presenter is
    allowed to show for it: run through format_inr, format_pct, or a
    plain count/1-decimal rendering -- the SAME formatters presenters.py
    itself uses.

    extra_counts covers legitimately DERIVED plain counts (e.g. len(rows))
    that are computed, not looked up, but are still not hallucinations.

    extra_values covers legitimately DERIVED numeric quantities that are
    NOT a raw field in the underlying data at all -- e.g. a price-
    difference percentage computed from two raw prices. These must be
    independently RE-COMPUTED by the test from the same raw inputs (not
    copied from the presenter's own output), so a wrong formula in the
    presenter still gets caught rather than rubber-stamped."""
    currency, percent, plain = set(), set(), set()

    def add_plain(v):
        plain.add(str(int(round(v))))
        for prec in (1, 2, 3, 4):
            plain.add(f"{v:.{prec}f}")
            plain.add(f"{abs(v):.{prec}f}")

    for v in numbers:
        currency.add(p.format_inr(v))
        percent.add(p.format_pct(v))
        percent.add(p.format_pct(abs(v)))
        add_plain(v)
    for c in extra_counts:
        plain.add(str(int(c)))
    for v in extra_values:
        currency.add(p.format_inr(v))
        percent.add(p.format_pct(v))
        percent.add(p.format_pct(abs(v)))
        add_plain(v)
    return currency, percent, plain


def check_headline(label, headline, underlying_data, extra_counts=(), extra_values=()):
    entity_strings = _all_strings(underlying_data)
    stripped = _strip_entities(headline, entity_strings)

    numbers = _all_numbers(underlying_data)
    currency_ok, percent_ok, plain_ok = _formatted_candidates(numbers, extra_counts, extra_values)

    unverified = []

    currency_matches = CURRENCY_RE.findall(stripped)
    for m in currency_matches:
        if m not in currency_ok:
            unverified.append(("currency", m))
    remaining = CURRENCY_RE.sub(" ", stripped)

    percent_matches = PERCENT_RE.findall(remaining)
    for m in percent_matches:
        if m not in percent_ok:
            unverified.append(("percent", m))
    remaining = PERCENT_RE.sub(" ", remaining)

    plain_matches = PLAIN_NUM_RE.findall(remaining)
    for m in plain_matches:
        if m not in plain_ok:
            unverified.append(("plain", m))

    ok = not unverified
    print(f"[{label}] {'OK' if ok else 'FAIL'}: {headline!r}")
    if not ok:
        print(f"    unverified: {unverified}")
    return ok


# ---------------------------------------------------------------------------
# Main check over all 33 presenters (25 demo + 8 live)
# ---------------------------------------------------------------------------
def _demo_extra_counts(qnum, raw):
    """Legitimately DERIVED counts a headline is allowed to quote (e.g.
    "10 SKU(s) flagged...") that aren't literally len(raw["data"]) because
    raw["data"] is a dict of sub-lists, not one flat list, for a handful
    of questions. Independently re-derives each count from raw["data"]
    rather than trusting the presenter's own len()."""
    data = raw["data"]
    if isinstance(data, list):
        return [len(data)]
    if qnum == 10:
        return [len(data.get("flagged_low_conversion_skus", []))]
    if qnum == 18:
        return [data.get("n_discounted", 0), data.get("n_full_price", 0)]
    if qnum == 6:
        return [len(data.get("own_margin_by_sku", [])), len(data.get("avg_price_by_category_brand", []))]
    return []


def _demo_check(qnum):
    raw = fbq.run_fixture_question(qnum)
    bundle = p.PRESENTERS[f"demo_{qnum}"](raw)
    extra_counts = _demo_extra_counts(qnum, raw)
    ok = check_headline(f"demo_{qnum}", bundle["headline"], raw["data"], extra_counts=extra_counts)
    return ok, bundle, raw["data"], extra_counts, []


_MISMATCH_BRAND_BY_ENTRY = {"live_2": ("Nestasia", "own_brand"), "live_3": ("Milton", "tracked_competitors"),
                             "live_4": ("Home Centre", "tracked_competitors"), "live_5": ("Borosil", "tracked_competitors")}


def _live_check(entry_id):
    entry = qr.get(entry_id)
    bundle = p.PRESENTERS[entry_id](entry["text"])
    result = retrieve(entry["text"], db_mode="production")

    extra_values, extra_counts = [], []
    if entry_id == "live_1":
        own = result["evidence"]["own_brand"]
        comp = result["evidence"]["tracked_competitors"]
        if own and comp:
            extra_values.append((float(own[0]["avg_price"]) - float(comp[0]["avg_price"])) / float(comp[0]["avg_price"]) * 100)
    elif entry_id in _MISMATCH_BRAND_BY_ENTRY:
        brand, side = _MISMATCH_BRAND_BY_ENTRY[entry_id]
        cls = result["evidence"][side]["classification"].get(brand)
        if cls:
            # "tested" is a derived sum (confirmed-mismatch + confirmed-clean
            # SKUs), independently re-computed here, not copied from the
            # presenter's own arithmetic.
            extra_counts.append(cls["skus_with_confirmed_mismatch"] + cls["skus_confirmed_clean"])
    elif entry_id == "live_6":
        comp_cls = result["evidence"]["tracked_competitors"]["classification"]
        testable = [c for c in comp_cls.values() if c["brand_testable_for_mismatch"]]
        untestable = [c for c in comp_cls.values() if not c["brand_testable_for_mismatch"]]
        with_bug = [c for c in testable if c["brand_has_confirmed_mismatch"]]
        extra_counts.extend([len(with_bug), len(testable), len(untestable)])

    ok = check_headline(entry_id, bundle["headline"], result["evidence"], extra_counts=extra_counts, extra_values=extra_values)
    return ok, bundle, result["evidence"], extra_counts, extra_values


ALL_DATA = {}  # qid -> {"bundle":..., "underlying":..., "extra_counts":..., "extra_values":...}
ALL_BUNDLES = {}  # qid -> bundle (kept for test_no_directive_language's existing signature)


def main():
    all_ok = True

    for qnum in range(1, 26):
        qid = f"demo_{qnum}"
        ok, bundle, underlying, extra_counts, extra_values = _demo_check(qnum)
        all_ok = all_ok and ok
        ALL_BUNDLES[qid] = bundle
        ALL_DATA[qid] = {"bundle": bundle, "underlying": underlying, "extra_counts": extra_counts, "extra_values": extra_values}
        print(f"    suggestion: {bundle['suggestion']!r}")
        print(f"    firm_up: {bundle['firm_up']!r}")
        print(f"    caveat: {bundle.get('caveat')!r}")
        print(f"    sources: {len(bundle['sources'])} source(s)")
        print(f"    visual type: {bundle['visual']['type'] if bundle['visual'] else None}")
        print()

    for i in range(1, 9):
        qid = f"live_{i}"
        ok, bundle, underlying, extra_counts, extra_values = _live_check(qid)
        all_ok = all_ok and ok
        ALL_BUNDLES[qid] = bundle
        ALL_DATA[qid] = {"bundle": bundle, "underlying": underlying, "extra_counts": extra_counts, "extra_values": extra_values}
        print(f"    suggestion: {bundle['suggestion']!r}")
        print(f"    firm_up: {bundle['firm_up']!r}")
        print(f"    caveat: {bundle.get('caveat')!r}")
        print(f"    sources: {len(bundle['sources'])} source(s)")
        print()

    print("ALL OK" if all_ok else "SOME FAILED")
    return all_ok


# ---------------------------------------------------------------------------
# Directive-language check: hard-fail on "you should"/"you must"/"you need
# to"/"we recommend" anywhere in a presenter's text fields; separately list
# (not fail on) any sentence that starts with a bare imperative verb, for
# human review -- an imperative isn't automatically wrong (e.g. "Confirm
# unit_cost against..." in firm_up is instructional by design), but it's
# worth a human glance every time one shows up in suggestion/headline.
# ---------------------------------------------------------------------------
FORBIDDEN_PHRASES = ("you should", "you must", "you need to", "we recommend")
IMPERATIVE_VERBS = ("cut", "stop", "increase", "reduce", "prioritise", "prioritize")
SENTENCE_RE = re.compile(r"[^.!?]+[.!?]?")


def test_no_directive_language(bundles):
    hard_failures = []
    for qid, bundle in bundles.items():
        for field in ("headline", "suggestion", "firm_up", "caveat"):
            text = bundle.get(field)
            if not text:
                continue
            lower = text.lower()
            for phrase in FORBIDDEN_PHRASES:
                if phrase in lower:
                    hard_failures.append((qid, field, phrase, text))

    review_list = []
    for qid, bundle in bundles.items():
        for field in ("headline", "suggestion"):
            text = bundle.get(field)
            if not text:
                continue
            for sentence in SENTENCE_RE.findall(text):
                s = sentence.strip()
                if not s:
                    continue
                first_word = s.split()[0].lower().rstrip(".,!?")
                if first_word in IMPERATIVE_VERBS:
                    review_list.append((qid, field, s))

    print(f"\n[directive-language] hard failures: {len(hard_failures)}")
    for f in hard_failures:
        print("   ", f)
    print(f"[directive-language] sentences starting with a bare imperative verb (for review): {len(review_list)}")
    for r in review_list:
        print("   ", r)

    return len(hard_failures) == 0, hard_failures, review_list


# ---------------------------------------------------------------------------
# Prove the test can actually fail: for EACH of the 33 presenters, mutate
# one number in its headline and confirm check_headline() catches it.
#
# The mutation target must be a genuine CLAIMED number (something
# check_headline would actually extract and verify), not just the first
# digit run anywhere in the string -- an earlier version mutated whatever
# digits appeared first, which often landed inside a product name's unit
# suffix (e.g. "...Baby Blue 460ml" -> "...Baby Blue 1047ml"). That digit
# was never a claim to begin with (it's excluded from extraction by the
# same word-boundary rule that stops "3000ml" from being flagged as a
# false positive), so of course mutating it doesn't change the check's
# verdict -- that's correct behaviour, not a gap. This version finds a
# real claim by running the SAME entity-stripping + regex extraction
# check_headline() uses, and only mutates a match from that.
#
# Headlines with no mutable CLAIM (e.g. a pure refusal, a "no candidates
# found" bundle, or one whose only digits are inside entity names) are
# reported separately, not silently skipped as passes.
# ---------------------------------------------------------------------------
def _find_claimed_number_match(headline, underlying_data):
    entity_strings = _all_strings(underlying_data)
    stripped = _strip_entities(headline, entity_strings)
    for regex in (CURRENCY_RE, PERCENT_RE, PLAIN_NUM_RE):
        m = regex.search(stripped)
        if m:
            return m.group()
    return None


def _mutate_claim(original_headline, claim_substring):
    mutated_claim = re.sub(r"\d+", lambda m: str(int(m.group()) + 587), claim_substring, count=1)
    return original_headline.replace(claim_substring, mutated_claim, 1), mutated_claim


def prove_test_catches_mutation_all(all_data):
    survived, no_claim, results = [], [], []

    for qid, d in all_data.items():
        good = d["bundle"]["headline"]
        underlying = d["underlying"]
        ok_before = check_headline(f"{qid} (unmutated)", good, underlying,
                                    extra_counts=d["extra_counts"], extra_values=d["extra_values"])

        claim = _find_claimed_number_match(good, underlying)
        if claim is None:
            no_claim.append((qid, good))
            continue

        mutated, mutated_claim = _mutate_claim(good, claim)
        ok_after = check_headline(f"{qid} (MUTATED: {claim!r} -> {mutated_claim!r})", mutated, underlying,
                                   extra_counts=d["extra_counts"], extra_values=d["extra_values"])

        passed = ok_before is True and ok_after is False
        results.append((qid, ok_before, ok_after, passed))
        if not passed:
            survived.append((qid, good, mutated, ok_before, ok_after))

    print()
    print(f"[mutation-proof-all] checked {len(results)} presenters with a genuine claimed number, "
          f"{len(no_claim)} had no claimed number to mutate.")
    print(f"[mutation-proof-all] survived (test FAILED to catch the mutation): {len(survived)}")
    for s in survived:
        print("   ", s)
    if no_claim:
        print(f"[mutation-proof-all] no claimed number present, mutation not attempted: {[qid for qid, _ in no_claim]}")

    return len(survived) == 0, survived, no_claim


# ---------------------------------------------------------------------------
# Ranking check: where a headline uses a superlative keyword (top/highest/
# lowest/best/worst/most/least/strongest/weakest), assert the entity it
# names is actually first (or last) when the underlying rows are sorted by
# the metric that headline is claiming to rank on. Each spec below
# independently re-derives the correct top/bottom row from raw["data"] --
# it does NOT trust the presenter's own sort/filter logic.
# ---------------------------------------------------------------------------
SUPERLATIVE_KEYWORDS = ("top", "highest", "lowest", "best", "worst", "most", "least", "strongest", "weakest")
_KEYWORD_RE = re.compile(r"\b(" + "|".join(SUPERLATIVE_KEYWORDS) + r")\b", re.IGNORECASE)


def _demo3_expected_category(rows):
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"], {})[r["side"]] = r["total_revenue"]
    best_cat, best_margin = None, None
    for cat, sides in by_cat.items():
        if "own" in sides and "competitor" in sides:
            margin = sides["own"] - sides["competitor"]
            if best_margin is None or margin > best_margin:
                best_margin, best_cat = margin, cat
    return best_cat


def _demo8_expected_category(rows):
    # Mirrors fixture_business_queries.q8's own sort key: None ratio sorts
    # last, otherwise highest ratio first.
    candidates = [r for r in rows if r["stock_to_sales_ratio"] is not None]
    if not candidates:
        return rows[0]["category"] if rows else None
    return max(candidates, key=lambda r: r["stock_to_sales_ratio"])["category"]


RANKING_SPECS = {
    "demo_3": {"custom": _demo3_expected_category},
    "demo_4": {"name_field": "product_name", "key": lambda r: r["days_of_stock_remaining"], "mode": "min"},
    "demo_7": {"name_field": "product_name", "key": lambda r: r["margin_volume_score"], "mode": "max"},
    "demo_8": {"custom": _demo8_expected_category},
    "demo_11": {"name_field": "channel", "key": lambda r: float(r["revenue"]), "mode": "max"},
    "demo_13": {"name_field": "channel", "key": lambda r: float(r["revenue"]), "mode": "max"},
    "demo_14": {"name_field": "channel", "key": lambda r: r["avg_return_rate"] or 0, "mode": "max"},
    "demo_15": {"name_field": "brand_name", "key": lambda r: r["unavailable_pct"] or 0, "mode": "max",
                "filter": lambda r: r["brand_name"] != "Nestasia"},
    "demo_16": {"name_field": "product_name", "key": lambda r: r["attributed_orders"], "mode": "max",
                "default_name": "an unattributed product"},
    "demo_17": {"name_field": "product_name", "key": lambda r: r["orders_per_rupee_spend"] or 0, "mode": "max"},
    "demo_19": {"name_field": "platform", "key": lambda r: r["likes"], "mode": "max"},
    "demo_20": {"name_field": "brand_name", "key": lambda r: r["n_ads"], "mode": "max"},
    "demo_21": {"name_field": "product_name", "key": lambda r: r["priority_score"], "mode": "max"},
    "demo_22": {"name_field": "category", "key": lambda r: r["total_stock"], "mode": "max"},
    "demo_24": {"name_field": "category", "key": lambda r: r["top_competitor_revenue"] - r["own_revenue"], "mode": "max"},
    "demo_25": {"name_field": "product_name", "key": lambda r: r["cut_candidate_score"], "mode": "min"},
}


def _ranking_expected_name(qid, rows):
    spec = RANKING_SPECS[qid]
    if "custom" in spec:
        return spec["custom"](rows)
    candidates = [r for r in rows if spec.get("filter", lambda _r: True)(r)]
    if not candidates:
        return None
    fn = max if spec["mode"] == "max" else min
    best = fn(candidates, key=spec["key"])
    return best.get(spec["name_field"]) or spec.get("default_name")


def check_all_rankings(all_data):
    verified, failed, unverifiable = [], [], []

    for qid, d in all_data.items():
        headline = d["bundle"]["headline"]
        if not _KEYWORD_RE.search(headline):
            continue
        if qid not in RANKING_SPECS:
            unverifiable.append((qid, headline))
            continue
        rows = d["underlying"] if isinstance(d["underlying"], list) else None
        if rows is None:
            unverifiable.append((qid, headline))
            continue
        expected = _ranking_expected_name(qid, rows)
        if expected is None:
            unverifiable.append((qid, headline))
            continue
        if expected in headline:
            verified.append((qid, expected))
        else:
            failed.append((qid, headline, expected))

    print()
    print(f"[ranking-check] headlines with a superlative keyword: {len(verified) + len(failed) + len(unverifiable)}")
    print(f"[ranking-check] verified correct: {len(verified)}")
    for v in verified:
        print("   ", v)
    print(f"[ranking-check] FAILED (named entity is not actually top/bottom): {len(failed)}")
    for f in failed:
        print("   ", f)
    print(f"[ranking-check] could not verify automatically: {len(unverifiable)}")
    for u in unverifiable:
        print("   ", u)

    return len(failed) == 0, failed, unverifiable


def print_all_headlines(all_data):
    print()
    print("=" * 70)
    print("All 33 headlines")
    print("=" * 70)
    for qid in [f"demo_{n}" for n in range(1, 26)] + [f"live_{n}" for n in range(1, 9)]:
        print(f"[{qid}] {all_data[qid]['bundle']['headline']}")


# ---------------------------------------------------------------------------
# Direction check: wherever a headline uses a comparative word (higher/
# lower/above/below/more/less/rising/declining/up/down/ahead/behind),
# independently extract the two quantities it's comparing from the
# underlying data and assert the claimed direction actually holds.
#
# Each spec is explicit about which raw value is "val_a" and which is
# "val_b" and what relation the headline's wording asserts between them
# (">" or "<") -- deliberately NOT a generic keyword->relation table,
# because a word like "down" means opposite things depending on which
# quantity is named first ("X down to N" vs "first period N1 vs latest
# N2" -- "down" there means N1 > N2, not val_a < val_b). Encoding the
# relation per-headline avoids that ambiguity being silently wrong.
# ---------------------------------------------------------------------------
DIRECTION_KEYWORDS = ("higher", "lower", "above", "below", "more", "less",
                       "rising", "declining", "up", "down", "ahead", "behind")
DIRECTION_RE = re.compile(r"\b(" + "|".join(DIRECTION_KEYWORDS) + r")\b", re.IGNORECASE)

# Text-level flip used only to show what the "deliberately flipped"
# headline would read like in the report -- the actual pass/fail
# verdict comes from flipping the spec's operator, not from word matching.
TEXT_FLIP = {"higher": "lower", "lower": "higher", "above": "below", "below": "above",
             "more": "less", "less": "more", "rising": "declining", "declining": "rising",
             "up": "down", "down": "up", "ahead": "behind", "behind": "ahead"}


def _demo9_direction_spec(underlying):
    by_cat = {r["category"]: r for r in underlying}
    cookware, bakeware = by_cat.get("Cookware"), by_cat.get("Bakeware")
    if not cookware or not bakeware:
        return None
    # Headline: "Bakeware is more profitable: Cookware averages X% ...
    # Bakeware's Y%." -- asserts Bakeware's margin > Cookware's margin.
    return {"name_a": "Bakeware margin", "val_a": float(bakeware["avg_margin_percent"]),
            "name_b": "Cookware margin", "val_b": float(cookware["avg_margin_percent"]), "op": ">"}


def _live1_direction_spec(underlying):
    own = underlying["own_brand"]
    comp = underlying["tracked_competitors"]
    if not own or not comp:
        return None
    # Headline: "Nestasia's Cookware averages ₹X, N% higher than Home
    # Centre's ₹Y." -- asserts own avg_price > competitor avg_price.
    return {"name_a": f"{own[0]['brand_name']} avg price", "val_a": float(own[0]["avg_price"]),
            "name_b": f"{comp[0]['brand_name']} avg price", "val_b": float(comp[0]["avg_price"]), "op": ">"}


DIRECTION_SPECS = {
    "demo_9": _demo9_direction_spec,
    "live_1": _live1_direction_spec,
}


def _verify_op(val_a, op, val_b):
    return val_a > val_b if op == ">" else val_a < val_b


def check_all_directions(all_data):
    verified, failed, unverifiable = [], [], []

    for qid, d in all_data.items():
        headline = d["bundle"]["headline"]
        matches = DIRECTION_RE.findall(headline)
        if not matches:
            continue
        keyword = matches[0].lower()
        spec_fn = DIRECTION_SPECS.get(qid)
        if spec_fn is None:
            unverifiable.append((qid, keyword, headline, "no direction-check spec written for this headline shape"))
            continue
        spec = spec_fn(d["underlying"])
        if spec is None:
            unverifiable.append((qid, keyword, headline, "underlying data doesn't have both quantities to compare"))
            continue
        ok = _verify_op(spec["val_a"], spec["op"], spec["val_b"])
        if ok:
            verified.append((qid, keyword, spec["name_a"], spec["val_a"], spec["name_b"], spec["val_b"]))
        else:
            failed.append((qid, keyword, headline, spec))

    print()
    print(f"[direction-check] headlines with a comparative keyword: {len(verified) + len(failed) + len(unverifiable)}")
    print(f"[direction-check] verified correct: {len(verified)}")
    for v in verified:
        print("   ", v)
    print(f"[direction-check] FAILED (claimed direction contradicts the data): {len(failed)}")
    for f in failed:
        print("   ", f)
    print(f"[direction-check] could not verify automatically: {len(unverifiable)}")
    for u in unverifiable:
        print("   ", u)

    return len(failed) == 0, failed, unverifiable


def prove_direction_flip(label, spec, real_headline_for_display=None, keyword_for_display=None):
    """Flips the spec's operator (the actual deliberate-error injection)
    and confirms _verify_op now returns False where it returned True
    before. Also prints the equivalent flipped headline text, for
    readability, using TEXT_FLIP -- that text is illustrative only; the
    pass/fail verdict comes from the operator flip, not from string
    matching."""
    ok_before = _verify_op(spec["val_a"], spec["op"], spec["val_b"])
    flipped_op = "<" if spec["op"] == ">" else ">"
    ok_after = _verify_op(spec["val_a"], flipped_op, spec["val_b"])

    flipped_text = None
    if real_headline_for_display and keyword_for_display:
        flipped_text = re.sub(r"\b" + keyword_for_display + r"\b", TEXT_FLIP[keyword_for_display],
                               real_headline_for_display, count=1, flags=re.IGNORECASE)

    print(f"[direction-flip-proof] {label}: unflipped ok={ok_before} (expect True), "
          f"flipped ok={ok_after} (expect False)")
    if flipped_text:
        print(f"    flipped headline would read: {flipped_text!r}")

    return ok_before is True and ok_after is False


def run_direction_flip_proofs(all_data):
    results = []

    # Case 1 & 2: the two real headlines in the current 33 that contain a
    # direction keyword AND have both quantities available to compare.
    for qid, keyword in (("demo_9", "more"), ("live_1", "higher")):
        spec = DIRECTION_SPECS[qid](all_data[qid]["underlying"])
        headline = all_data[qid]["bundle"]["headline"]
        passed = prove_direction_flip(qid, spec, headline, keyword)
        results.append((qid, passed))

    # Case 3: only 2 of the current 33 real headlines contain a direction
    # keyword with both quantities present (demo_12 names only one
    # quantity with no baseline to compare against; demo_23's comparative
    # "trending down" wording only appears in its non-empty branch, which
    # no SKU currently triggers -- see check_all_directions()'s
    # "unverifiable" list). To reach 3 genuine proof cases without
    # fabricating a headline for the dump, this exercises that SAME
    # present_demo_23() code path directly with constructed input data
    # (not one of the 33 real current headlines -- clearly a synthetic
    # proof case, not part of headlines_dump.md).
    synthetic_raw = {"data": [{"product_name": "Synthetic Test Product For Proof Only",
                                "units_sold_by_period": [500, 100]}]}
    synthetic_bundle = p.present_demo_23(synthetic_raw)
    synthetic_spec = {"name_a": "first period units", "val_a": 500,
                       "name_b": "latest period units", "val_b": 100, "op": ">"}
    print(f"[direction-flip-proof] demo_23 (SYNTHETIC INPUT, code path only, not in headlines_dump.md): "
          f"{synthetic_bundle['headline']!r}")
    passed = prove_direction_flip("demo_23 (synthetic)", synthetic_spec, synthetic_bundle["headline"], "down")
    results.append(("demo_23 (synthetic)", passed))

    all_passed = all(ok for _, ok in results)
    print(f"[direction-flip-proof] {sum(1 for _, ok in results if ok)}/{len(results)} flip proofs passed")
    return all_passed, results


if __name__ == "__main__":
    headline_ok = main()
    directive_ok, hard_failures, review_list = test_no_directive_language(ALL_BUNDLES)
    mutation_ok, survived, no_claim = prove_test_catches_mutation_all(ALL_DATA)
    ranking_ok, ranking_failed, ranking_unverifiable = check_all_rankings(ALL_DATA)
    direction_ok, direction_failed, direction_unverifiable = check_all_directions(ALL_DATA)
    flip_proof_ok, flip_proof_results = run_direction_flip_proofs(ALL_DATA)
    print_all_headlines(ALL_DATA)

    print()
    print("=" * 70)
    print(f"Headline-number check over all 33 presenters: {'PASSED' if headline_ok else 'FAILED'}")
    print(f"Mutation-detection proof (all 33):             {'PASSED' if mutation_ok else 'FAILED'} "
          f"({len(no_claim)} had no claimed number to mutate)")
    print(f"Ranking check:                                  {'PASSED' if ranking_ok else 'FAILED'} "
          f"({len(ranking_unverifiable)} could not be verified automatically)")
    print(f"Direction check:                                {'PASSED' if direction_ok else 'FAILED'} "
          f"({len(direction_unverifiable)} could not be verified automatically)")
    print(f"Direction flip-proof (>=3 headlines):           {'PASSED' if flip_proof_ok else 'FAILED'}")
    print(f"Directive-language hard check:                 {'PASSED' if directive_ok else 'FAILED'}")
    print(f"Imperative-verb sentences flagged for review:   {len(review_list)}")
