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
PLAIN_NUM_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")


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
    for v in numbers:
        currency.add(p.format_inr(v))
        percent.add(p.format_pct(v))
        percent.add(p.format_pct(abs(v)))
        plain.add(str(int(round(v))))
        plain.add(f"{v:.1f}")
        plain.add(f"{v:.2f}")
        plain.add(f"{abs(v):.1f}")
    for c in extra_counts:
        plain.add(str(int(c)))
    for v in extra_values:
        currency.add(p.format_inr(v))
        percent.add(p.format_pct(v))
        percent.add(p.format_pct(abs(v)))
        plain.add(str(int(round(v))))
        plain.add(f"{v:.1f}")
        plain.add(f"{abs(v):.1f}")
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
    ok = check_headline(f"demo_{qnum}", bundle["headline"], raw["data"], extra_counts=_demo_extra_counts(qnum, raw))
    return ok, bundle


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
    return ok, bundle


ALL_BUNDLES = {}  # populated by main(), reused by test_no_directive_language()


def main():
    all_ok = True

    for qnum in range(1, 26):
        ok, bundle = _demo_check(qnum)
        all_ok = all_ok and ok
        ALL_BUNDLES[f"demo_{qnum}"] = bundle
        print(f"    suggestion: {bundle['suggestion']!r}")
        print(f"    firm_up: {bundle['firm_up']!r}")
        print(f"    caveat: {bundle.get('caveat')!r}")
        print(f"    sources: {len(bundle['sources'])} source(s)")
        print(f"    visual type: {bundle['visual']['type'] if bundle['visual'] else None}")
        print()

    for i in range(1, 9):
        entry_id = f"live_{i}"
        ok, bundle = _live_check(entry_id)
        all_ok = all_ok and ok
        ALL_BUNDLES[entry_id] = bundle
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
# Prove the test can actually fail: mutate one number in a real headline
# and confirm check_headline() catches it.
# ---------------------------------------------------------------------------
def prove_test_catches_mutation():
    raw = fbq.run_fixture_question(1)
    bundle = p.PRESENTERS["demo_1"](raw)
    good = bundle["headline"]
    ok_before = check_headline("demo_1 (unmutated)", good, raw["data"], extra_counts=[len(raw["data"])])

    # Mutate the units-sold figure to a value that does not appear anywhere
    # in the underlying data (add 587 to make collision with a real number
    # vanishingly unlikely).
    mutated = re.sub(r"\b\d+\b", lambda m: str(int(m.group()) + 587), good, count=1)
    ok_after = check_headline("demo_1 (MUTATED)", mutated, raw["data"], extra_counts=[len(raw["data"])])

    print()
    print(f"Mutation proof -- unmutated headline passed: {ok_before} (expected True)")
    print(f"Mutation proof -- mutated headline passed:   {ok_after} (expected False)")
    return ok_before is True and ok_after is False


if __name__ == "__main__":
    headline_ok = main()
    proof_ok = prove_test_catches_mutation()
    print("MUTATION PROOF: " + ("PASSED" if proof_ok else "FAILED -- the test cannot detect a wrong number!"))
    directive_ok, hard_failures, review_list = test_no_directive_language(ALL_BUNDLES)

    print()
    print("=" * 70)
    print(f"Headline-number check over all 33 presenters: {'PASSED' if headline_ok else 'FAILED'}")
    print(f"Mutation-detection proof:                      {'PASSED' if proof_ok else 'FAILED'}")
    print(f"Directive-language hard check:                 {'PASSED' if directive_ok else 'FAILED'}")
    print(f"Imperative-verb sentences flagged for review:   {len(review_list)}")
