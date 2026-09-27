"""
Runs the fixed 8-question test set from the brief through generate.py and
prints each answer alongside the known-correct answer, so a human can
compare them side by side rather than re-deriving the ground truth by hand
each time.

Known-correct answers here are NOT hardcoded expectations the script checks
programmatically -- they're printed for a human reviewer to compare against
what the model actually said, per the brief's "report actual model output
... against known correct answers" instruction. Grading whether an LLM
answer is right, wrong, or acceptably-hedged still needs a human judgment
call, especially for question 6.

Requires Ollama installed and running with the model pulled -- see
generate.py's check_ollama().

Usage:
    python test_questions.py
"""
from generate import check_ollama, generate

QUESTIONS = [
    (
        "How does our Cookware pricing compare to Home Centre's?",
        "Nestasia Cookware avg ~Rs 1775 (12 SKUs, complete); Home Centre Cookware "
        "avg ~Rs 1174 (259 SKUs, complete) -- Home Centre is cheaper on average and "
        "has a far larger assortment.",
    ),
    (
        "Which of our products show a stock-display inconsistency?",
        "As of the current complete data (Cookware, Bakeware, Kitchen Racks+Trivets), "
        "ZERO confirmed mismatches -- 0 of 75 tested SKUs. Container and Lunch "
        "Boxes+Bags are still PARTIAL/incomplete, so this is not a full-catalogue "
        "answer. (Note: this differs from earlier founder/manual research that "
        "documented mismatches in Trivets/Kitchen Racks/Bakeware -- the live re-test "
        "in this database hasn't reproduced those in its current samples.)",
    ),
    (
        "Does Milton have the same stock-display bug we do?",
        "Milton: YES, confirmed -- 6 real mismatches (has_confirmed_bug=true), "
        "complete data. Nestasia's OWN current data shows 0 confirmed mismatches in "
        "its complete categories, so 'the bug we do' should be qualified, not stated "
        "as settled -- see question 2's caveat.",
    ),
    (
        "Does Home Centre have the stock-display bug?",
        "CANNOT BE TESTED -- Home Centre's architecture (category-browse API filters "
        "to in-stock only, no add-to-cart control on the grid) means there is no "
        "on-page signal to check. testable=false. This must NOT be answered as 'no' "
        "or 'clean'.",
    ),
    (
        "Does Borosil have this bug too?",
        "YES, confirmed -- 3 real mismatches (has_confirmed_bug=true), complete data, "
        "collected 2026-09-26 (the freshest data in the whole database).",
    ),
    (
        "How many of our tracked competitors have this stock-display bug?",
        "2 of 5 tracked competitors confirmed: Milton and Borosil. Prestige and "
        "Wonderchef were tested and confirmed CLEAN (2 of 5). Home Centre (1 of 5) "
        "cannot be tested at all and must be reported separately, not folded into "
        "either the 'has bug' or 'clean' count.",
    ),
    (
        "Which Container collections have incomplete data right now?",
        "Nestasia's Container category: PARTIAL, 26 SKUs collected so far, "
        "collection_complete=false.",
    ),
    (
        "What's our best-selling SKU in Cookware?",
        "MUST REFUSE -- no sales data exists in this schema at all (SKU/price/stock/"
        "ad data only).",
    ),
]


def main():
    ok, msg = check_ollama()
    print(msg)
    if not ok:
        return

    for i, (question, known_correct) in enumerate(QUESTIONS, start=1):
        print()
        print("#" * 70)
        print(f"Q{i}: {question}")
        print("#" * 70)
        result = generate(question)
        print("MODEL ANSWER:")
        print(result["answer"])
        print()
        print(f"[safety check: {'PASSED' if result['safety_check_passed'] else 'FLAGGED'} "
              f"-- {result['safety_check_reason']}]")
        print()
        print("KNOWN CORRECT / EXPECTED SHAPE:")
        print(known_correct)


if __name__ == "__main__":
    main()
