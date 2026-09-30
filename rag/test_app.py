"""
Drives app.py with streamlit.testing.v1.AppTest -- no browser needed.

Covers:
  1. Every dropdown question in both modes (34 total) -- asserts each
     rendered card has a mode label, both badges (Data/Coverage), at
     least one visible line under Sources, and a disclaimer line.
  2. 3 known-good paraphrases + 2 nonsense inputs through the free-text
     box -- asserts the 3 match and answer, the 2 show the unsupported
     list and appear in the JSONL log.
  3. None of the internal field names (collection_complete, is_synthetic,
     source_type, own_brand, tracked_competitors) appears anywhere in a
     card's user-facing fields (headline/suggestion/disclaimer/firm_up/
     sources) across all 34 questions -- checked at the data level (the
     exact fields render_card() renders outside the Details expander),
     which is a more precise check than parsing rendered DOM text, since
     it tests the same data the renderer consumes rather than hoping a
     later markup change doesn't accidentally leak something.

Usage:
    python test_app.py
"""
import json
import re

from streamlit.testing.v1 import AppTest

import question_registry as qr

FORBIDDEN_INTERNAL_NAMES = ["collection_complete", "is_synthetic", "source_type", "own_brand", "tracked_competitors"]

# Belt-and-suspenders: beyond the 5 named terms above, ANY snake_case-looking
# token (e.g. margin_percent, unit_cost, sku_id) is almost certainly a raw
# DB/field name that leaked into user-facing text -- catches this whole class
# of leak (found the hard way: a new UI feature exposed gap_explanation/notes
# text nobody had scanned before) without needing to name each one up front.
SNAKE_CASE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")

PARAPHRASE_CASES = [
    ("Do we have dead stock eating up our cash?", "demo", "demo_2"),
    ("Which channel makes us the most money?", "demo", "demo_11"),
    ("Is Borosil also affected by the stock-display bug?", "live", "live_5"),
]

NONSENSE_CASES = [
    "what's the weather today?",
    "who is our best influencer?",
]


def _new_app():
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    return at


def _select_mode(at, mode):
    at.session_state["mode"] = mode
    at.run(timeout=30)


def _pick_dropdown(at, mode, option_text):
    at.sidebar.selectbox[0].select(option_text).run(timeout=240)
    return at.session_state["current_card"]


def _ask_freetext(at, mode, text):
    _select_mode(at, mode)
    at.sidebar.text_input[0].set_value(text)
    at.sidebar.button[0].click().run(timeout=240)
    return at.session_state["current_card"]


def _card_public_text(card):
    """Every string a user sees OUTSIDE the Details expander -- exactly
    the fields render_card() renders before it gets to st.expander()."""
    parts = [card.get("headline", ""), card.get("suggestion", ""), card.get("disclaimer", ""),
              card.get("firm_up", "") or "", card.get("data_chip", ""), card.get("coverage_chip", ""),
              card.get("coverage_missing", "") or ""]
    for s in card.get("sources", []):
        parts.append(s.get("label", "") or "")
    return " ".join(parts)


def test_all_dropdown_questions():
    at = _new_app()
    failures = []
    leaks = []

    # Demo: 25 questions, grouped by category in the dropdown.
    _select_mode(at, "demo")
    demo_options = at.sidebar.selectbox[0].options[1:]  # skip placeholder
    assert len(demo_options) == 25, f"expected 25 demo dropdown options, got {len(demo_options)}"
    for opt in demo_options:
        card = _pick_dropdown(at, "demo", opt)
        _check_card(card, opt, failures, leaks)

    # Live: 8 questions, ungrouped.
    _select_mode(at, "live")
    live_options = at.sidebar.selectbox[0].options[1:]
    assert len(live_options) == 9, f"expected 9 live dropdown options, got {len(live_options)}"
    for opt in live_options:
        card = _pick_dropdown(at, "live", opt)
        _check_card(card, opt, failures, leaks)

    print(f"[dropdown-questions] checked {len(demo_options) + len(live_options)} questions")
    print(f"[dropdown-questions] failures: {len(failures)}")
    for f in failures:
        print("   ", f)
    print(f"[internal-name-leaks] {len(leaks)}")
    for l in leaks:
        print("   ", l)
    return failures, leaks


def _check_card(card, option_text, failures, leaks):
    if card["outcome"] == "ollama_down":
        failures.append((option_text, "Ollama not reachable -- cannot verify this card"))
        return
    if card["outcome"] != "matched":
        failures.append((option_text, f"unexpected outcome: {card['outcome']}"))
        return
    if not card.get("mode"):
        failures.append((option_text, "missing mode"))
    if not card.get("data_chip"):
        failures.append((option_text, "missing Data chip"))
    if not card.get("coverage_chip"):
        failures.append((option_text, "missing Coverage chip"))
    if not card.get("sources") and card["sources"] != []:
        failures.append((option_text, "sources field missing entirely"))
    elif not card["sources"]:
        # Empty sources list is only legitimate for the one true refusal
        # (live_8) -- everything else should cite something.
        if card["details"]["matched_id"] != "live_8":
            failures.append((option_text, "empty sources list on a non-refusal question"))
    if not card.get("disclaimer"):
        failures.append((option_text, "missing disclaimer line"))
    if "coverage_missing" not in card or not card["coverage_missing"]:
        failures.append((option_text, "missing Coverage 'Missing: ...' line"))

    public_text = _card_public_text(card)
    for name in FORBIDDEN_INTERNAL_NAMES:
        if name in public_text:
            leaks.append((option_text, name, public_text))
    for m in SNAKE_CASE_RE.finditer(public_text):
        if m.group(0) not in FORBIDDEN_INTERNAL_NAMES:
            leaks.append((option_text, m.group(0), public_text))


def test_freetext_paraphrases_and_nonsense():
    at = _new_app()
    match_failures = []
    for text, mode, expected_id in PARAPHRASE_CASES:
        card = _ask_freetext(at, mode, text)
        if card["outcome"] != "matched" or card["details"]["matched_id"] != expected_id:
            match_failures.append((text, expected_id, card))
        else:
            print(f"[paraphrase] OK: {text!r} -> {card['details']['matched_id']}")

    unsupported_failures = []
    for text in NONSENSE_CASES:
        card = _ask_freetext(at, "demo", text)
        if card["outcome"] != "unsupported":
            unsupported_failures.append((text, card))
        else:
            print(f"[nonsense] OK: {text!r} -> unsupported list shown")

    # Confirm both nonsense inputs actually landed in the JSONL log.
    log_failures = []
    log_path = at.session_state.get("_log_path")  # not set by app.py; read the real file instead
    import os
    real_log_path = os.path.join(os.path.dirname(__file__), ".interaction_log.jsonl")
    logged_texts = set()
    if os.path.exists(real_log_path):
        with open(real_log_path, encoding="utf-8") as f:
            for line in f:
                try:
                    logged_texts.add(json.loads(line)["typed_text"])
                except Exception:
                    continue
    for text in NONSENSE_CASES:
        if text not in logged_texts:
            log_failures.append(text)

    print(f"[paraphrase] failures: {len(match_failures)}")
    for f in match_failures:
        print("   ", f)
    print(f"[nonsense] failures: {len(unsupported_failures)}")
    for f in unsupported_failures:
        print("   ", f)
    print(f"[jsonl-log] nonsense inputs missing from log: {len(log_failures)}")
    for f in log_failures:
        print("   ", f)

    return match_failures, unsupported_failures, log_failures


def test_unsupported_list_renders_in_full():
    """The unsupported card's grouped_questions must list EVERY question
    registered for that mode -- not a truncated preview. Checked by count
    against question_registry.all_for_mode(), independently of render_card's
    own loop (which has no length limit in its code, but this proves it at
    the data level the renderer actually consumes)."""
    at = _new_app()
    failures = []
    for mode, expected_count in (("demo", 25), ("live", 9)):
        card = _ask_freetext(at, mode, "asdkjfhalskdjfh nonsense query zzz")
        if card["outcome"] != "unsupported":
            failures.append((mode, f"expected unsupported, got {card['outcome']}"))
            continue
        listed = sum(len(v) for v in card["grouped_questions"].values())
        if listed != expected_count:
            failures.append((mode, f"expected {expected_count} questions listed, got {listed}"))
        else:
            print(f"[unsupported-list-full] OK: {mode} lists all {listed} questions")
    print(f"[unsupported-list-full] failures: {len(failures)}")
    for f in failures:
        print("   ", f)
    return failures


if __name__ == "__main__":
    dropdown_failures, leaks = test_all_dropdown_questions()
    match_failures, unsupported_failures, log_failures = test_freetext_paraphrases_and_nonsense()
    unsupported_list_failures = test_unsupported_list_renders_in_full()

    print()
    print("=" * 70)
    print(f"Dropdown-question failures: {len(dropdown_failures)}")
    print(f"Internal-name leaks:        {len(leaks)}")
    print(f"Paraphrase-match failures:  {len(match_failures)}")
    print(f"Nonsense-unsupported failures: {len(unsupported_failures)}")
    print(f"Nonsense-not-logged failures:  {len(log_failures)}")
    print(f"Unsupported-list-not-full failures: {len(unsupported_list_failures)}")
    total = (len(dropdown_failures) + len(leaks) + len(match_failures) + len(unsupported_failures) +
             len(log_failures) + len(unsupported_list_failures))
    print(f"TOTAL FAILURES: {total}")
