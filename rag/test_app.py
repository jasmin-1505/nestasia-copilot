"""
Drives app.py with streamlit.testing.v1.AppTest -- no browser needed.
Asks all 25 demo questions in Demo mode and the 8 live questions in Live
mode, checks every rendered card has a mode label + both badges + a
citation + a disclaimer, replays the router's cross-mode test cases and
asserts none of them gets answered from the wrong source, and checks the
echo-back on 5 paraphrases.

Usage:
    python test_app.py
"""
from streamlit.testing.v1 import AppTest

import question_registry as qr

REQUIRED_FIELDS_MATCHED = ["mode", "accuracy", "scope", "scope_missing", "citation", "disclaimers"]

CROSS_MODE_CASES = [
    ("How does our Cookware pricing compare to Home Centre's?", "demo", "live"),
    ("Does Milton have the same stock-display bug we do?", "demo", "live"),
    ("What's our best-selling SKU in Cookware?", "demo", "live"),
    ("Which sales channel is driving the most revenue for us right now?", "live", "demo"),
    ("Should we run a promotion on a specific category to clear inventory?", "live", "demo"),
    ("If we had to cut 10% of our SKUs, which should go?", "live", "demo"),
]

# All 5 confirmed >= MATCH_THRESHOLD (0.671) in the router's own Step-4
# test set. Two additional known-hard cases (brand-swapped near-duplicate
# live questions) are included separately below to illustrate, honestly,
# where the router correctly falls back to unsupported rather than guess.
PARAPHRASE_ECHO_CASES = [
    ("Do we have dead stock eating up our cash?", "demo", "demo_2"),
    ("Which makes us more money, Cookware or Bakeware?", "demo", "demo_9"),
    ("Which channel makes us the most money?", "demo", "demo_11"),
    ("Is Borosil also affected by the stock-display bug?", "live", "live_5"),
    ("Which Cookware product sells the most units for us?", "live", "live_8"),
]

KNOWN_HARD_CASES = [
    ("How do our Bakeware prices stack up against Wonderchef?", "live"),
    ("Does Prestige show the same stock mismatch issue?", "live"),
]


def _new_app():
    at = AppTest.from_file("app.py")
    at.run(timeout=30)
    return at


def _ask(at, text, mode):
    """First call in a fresh AppTest pays a one-time ~60-90s cold-start cost
    (sentence-transformers model load inside AppTest's script-runner
    thread); every subsequent call on the SAME `at` instance is ~1-2s once
    the model and embedding cache are warm. Tests share one `at` across all
    of a suite's questions for exactly this reason."""
    at.session_state["mode"] = mode
    at.chat_input[0].set_value(text).run(timeout=120)
    return at.session_state["history"][-1]


def test_all_demo_questions(at):
    missing = []
    for n in range(1, 26):
        entry = qr.get(f"demo_{n}")
        card = _ask(at, entry["text"], "demo")
        if card["outcome"] != "matched":
            missing.append((f"demo_{n}", "did not match", card))
            continue
        for field in REQUIRED_FIELDS_MATCHED:
            if not card.get(field):
                missing.append((f"demo_{n}", f"missing/empty field: {field}", card))
        if not card["disclaimers"]:
            missing.append((f"demo_{n}", "empty disclaimers list", card))
    print(f"[demo 25] failures: {len(missing)}")
    for m in missing:
        print("  ", m)
    return missing


def test_all_live_questions(at):
    missing = []
    for i in range(1, 9):
        entry = qr.get(f"live_{i}")
        card = _ask(at, entry["text"], "live")
        if card["outcome"] not in ("matched", "ollama_down"):
            missing.append((f"live_{i}", "did not match", card))
            continue
        if card["outcome"] == "ollama_down":
            missing.append((f"live_{i}", "Ollama not reachable -- cannot verify card fields", card))
            continue
        for field in REQUIRED_FIELDS_MATCHED:
            if not card.get(field):
                missing.append((f"live_{i}", f"missing/empty field: {field}", card))
        if not card["disclaimers"]:
            missing.append((f"live_{i}", "empty disclaimers list", card))
    print(f"[live 8] failures: {len(missing)}")
    for m in missing:
        print("  ", m)
    return missing


def test_cross_mode_leaks(at):
    leaks = []
    for text, typed_mode, true_mode in CROSS_MODE_CASES:
        card = _ask(at, text, typed_mode)
        if card["outcome"] == "matched":
            leaks.append((text, typed_mode, "LEAKED -- answered as matched", card))
        elif card["outcome"] == "wrong_mode" and card["which_mode_answers_it"] != true_mode:
            leaks.append((text, typed_mode, "wrong_mode pointed at the wrong mode", card))
    print(f"[cross-mode] leaks: {len(leaks)}")
    for l in leaks:
        print("  ", l)
    return leaks


def test_echo_back(at):
    failures = []
    for text, mode, expected_id in PARAPHRASE_ECHO_CASES:
        card = _ask(at, text, mode)
        if card["outcome"] != "matched" or card["matched_id"] != expected_id:
            failures.append((text, expected_id, card))
        else:
            print(f"[echo] OK: {text!r} -> {card['question_text']!r} ({round(card['match_confidence']*100)}%)")
    print(f"[echo-back] failures: {len(failures)}")
    for f in failures:
        print("  ", f)

    for text, mode in KNOWN_HARD_CASES:
        card = _ask(at, text, mode)
        print(f"[echo, known-hard case, not asserted] {text!r} -> outcome={card['outcome']} "
              f"(brand-swapped near-duplicate question; see router report for why)")

    return failures


if __name__ == "__main__":
    at = _new_app()
    r1 = test_all_demo_questions(at)
    r2 = test_all_live_questions(at)
    r3 = test_cross_mode_leaks(at)
    r4 = test_echo_back(at)
    total_fail = len(r1) + len(r2) + len(r3) + len(r4)
    print()
    print(f"TOTAL FAILURES: {total_fail}")
