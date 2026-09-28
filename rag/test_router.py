"""
Tests router.py alone, no UI. Per the brief:
  - 2 paraphrases (1 close, 1 loose/colloquial) per demo question (50 cases)
  - >=2 paraphrases per live intent, using different brands (16 cases)
  - 8 plausible-but-unsupported questions
  - 6 cross-mode cases (a live question typed in demo mode, and the reverse)

Reports correct-match rate, false-accept rate, false-reject rate, and
cross-mode leak count. A false accept (wrong question answered, or an
unsupported question answered) is worse than a false reject, so the
threshold in router.py is tuned to zero false accepts and zero cross-mode
leaks on this set, even at the cost of some false rejects.

Usage:
    python test_router.py
"""
import router

# ---------------------------------------------------------------------------
# Demo paraphrases: (typed_text, expected_question_id, mode_to_query_in)
# ---------------------------------------------------------------------------
DEMO_CASES = [
    ("Which of our products should we prioritize promoting this quarter based on sales momentum and stock health?", "demo_1"),
    ("What should we push hardest this quarter based on how well it's selling and stock levels?", "demo_1"),
    ("Are we sitting on slow-moving inventory that's tying up cash right now?", "demo_2"),
    ("Do we have dead stock eating up our cash?", "demo_2"),
    ("Which product category is our strongest performer relative to competitors?", "demo_3"),
    ("Which category are we crushing the competition in?", "demo_3"),
    ("Do we have bestsellers at risk of going out of stock soon?", "demo_4"),
    ("Are any of our top sellers about to run out?", "demo_4"),
    ("Which recent launches are underperforming and might need repositioning?", "demo_5"),
    ("Which new products are flopping and might need a rethink?", "demo_5"),
    ("Are we leaving margin on the table by underpricing versus competitors anywhere?", "demo_6"),
    ("Are we pricing too low compared to rivals and losing margin?", "demo_6"),
    ("Which products have the best margin-to-volume ratio right now?", "demo_7"),
    ("What's giving us the best bang for buck between margin and sales volume?", "demo_7"),
    ("Should we run a promotion on a specific category to clear inventory?", "demo_8"),
    ("Is it time to put a category on sale to clear out stock?", "demo_8"),
    ("How does our profitability in Cookware compare to Bakeware?", "demo_9"),
    ("Which makes us more money, Cookware or Bakeware?", "demo_9"),
    ("Are any SKUs priced in a way that's hurting conversion?", "demo_10"),
    ("Is our pricing scaring people off before they buy?", "demo_10"),
    ("Which sales channel is driving the most revenue for us right now?", "demo_11"),
    ("Which channel makes us the most money?", "demo_11"),
    ("Are we out of stock on a high-demand product on any specific channel?", "demo_12"),
    ("Is a hot product sold out on one of our sales channels?", "demo_12"),
    ("Should we expand our qCommerce assortment based on how it's performing?", "demo_13"),
    ("Should we add more products to Blinkit/Zepto/Instamart given how they're doing?", "demo_13"),
    ("Which channel has our highest return/complaint rate?", "demo_14"),
    ("Where are customers complaining or returning stuff the most?", "demo_14"),
    ("Where are competitors outperforming us in availability?", "demo_15"),
    ("Where do rivals have better in-stock rates than us?", "demo_15"),
    ("Which ad campaign is actually driving sales, not just impressions?", "demo_16"),
    ("Which of our ads is actually moving product, not just getting views?", "demo_16"),
    ("Should we increase ad spend on a specific product given current trends?", "demo_17"),
    ("Should we spend more on ads for one of our products right now?", "demo_17"),
    ("Are festive/discount campaigns outperforming everyday pricing?", "demo_18"),
    ("Do festive sales actually beat regular pricing?", "demo_18"),
    ("What does social engagement tell us about what to promote next?", "demo_19"),
    ("Based on our social likes/comments, what should we push next?", "demo_19"),
    ("Which competitor promotional approach looks most effective right now?", "demo_20"),
    ("Whose marketing approach among competitors seems to be working best?", "demo_20"),
    ("Which single product should we prioritize across marketing, inventory, and pricing this month?", "demo_21"),
    ("If we could only focus on one product everywhere this month, which one?", "demo_21"),
    ("If we could fix only one thing in Kitchen this quarter, what and why?", "demo_22"),
    ("If we had one shot to fix something in our kitchen lineup, what would it be?", "demo_22"),
    ("Which product is quietly becoming a problem before it's obvious?", "demo_23"),
    ("What's silently turning into an issue before it's obvious?", "demo_23"),
    ("Where are we most vulnerable to a competitor taking share right now?", "demo_24"),
    ("Where could a rival most easily steal market share from us?", "demo_24"),
    ("If we had to cut 10% of our SKUs, which should go?", "demo_25"),
    ("If we trimmed 10 percent of our product line, what should we drop?", "demo_25"),
]

# ---------------------------------------------------------------------------
# Live paraphrases, >=2 per intent, using different brands where relevant
# ---------------------------------------------------------------------------
LIVE_CASES = [
    ("How does our Cookware pricing compare to Home Centre's?", "live_1"),
    ("How do our Bakeware prices stack up against Wonderchef?", "live_1"),
    ("Which of our products show a stock-display inconsistency?", "live_2"),
    ("Do any of our own listings have a stock mismatch bug?", "live_2"),
    ("Does Milton have the same stock-display bug we do?", "live_3"),
    ("Does Prestige show the same stock mismatch issue?", "live_3"),
    ("Does Home Centre have the stock-display bug?", "live_4"),
    ("Can we even test Home Centre for the stock mismatch bug?", "live_4"),
    ("Does Borosil have this bug too?", "live_5"),
    ("Is Borosil also affected by the stock-display bug?", "live_5"),
    ("How many of our tracked competitors have this stock-display bug?", "live_6"),
    ("Across all the competitors we track, how many show the bug?", "live_6"),
    ("Which Container collections have incomplete data right now?", "live_7"),
    ("What's still partially collected in the Container category?", "live_7"),
    ("What's our best-selling SKU in Cookware?", "live_8"),
    ("Which Cookware product sells the most units for us?", "live_8"),
]

# ---------------------------------------------------------------------------
# Plausible-but-unsupported (checked against BOTH modes)
# ---------------------------------------------------------------------------
UNSUPPORTED_CASES = [
    "What's our customer acquisition cost?",
    "Which product will sell most next Diwali?",
    "Who is our best influencer?",
    "What's the weather today?",
    "What's our net promoter score?",
    "How many people follow us on LinkedIn?",
    "What's our warehouse lease renewal date?",
    "Should we hire a new marketing director?",
]

# ---------------------------------------------------------------------------
# Cross-mode: (typed_text, mode_it_is_typed_in, expected_true_home_mode)
# ---------------------------------------------------------------------------
CROSS_MODE_CASES = [
    ("How does our Cookware pricing compare to Home Centre's?", "demo", "live"),
    ("Does Milton have the same stock-display bug we do?", "demo", "live"),
    ("What's our best-selling SKU in Cookware?", "demo", "live"),
    ("Which sales channel is driving the most revenue for us right now?", "live", "demo"),
    ("Should we run a promotion on a specific category to clear inventory?", "live", "demo"),
    ("If we had to cut 10% of our SKUs, which should go?", "live", "demo"),
]


def run():
    results = {"correct": 0, "false_accept": 0, "false_reject": 0, "cross_mode_leak": 0, "total": 0}
    failures = []

    def check(label, typed_text, mode, expect_outcome, expect_id=None, expect_other_mode=None):
        results["total"] += 1
        r = router.route(typed_text, mode)
        ok = r["outcome"] == expect_outcome
        if ok and expect_outcome == "matched":
            ok = r["question_id"] == expect_id
        if ok and expect_outcome == "wrong_mode":
            ok = r["which_mode_answers_it"] == expect_other_mode

        if ok:
            results["correct"] += 1
        else:
            if expect_outcome == "matched" and r["outcome"] != "matched":
                results["false_reject"] += 1
            elif expect_outcome == "matched" and r["outcome"] == "matched" and r["question_id"] != expect_id:
                results["false_accept"] += 1
            elif expect_outcome == "unsupported" and r["outcome"] != "unsupported":
                results["false_accept"] += 1
            elif expect_outcome == "wrong_mode" and r["outcome"] == "matched":
                results["cross_mode_leak"] += 1
                results["false_accept"] += 1
            elif expect_outcome == "wrong_mode" and r["outcome"] != "wrong_mode":
                results["false_reject"] += 1
            failures.append({"label": label, "typed_text": typed_text, "mode": mode,
                              "expected": (expect_outcome, expect_id or expect_other_mode),
                              "got": r})

    for text, qid in DEMO_CASES:
        check("demo-paraphrase", text, "demo", "matched", expect_id=qid)

    for text, qid in LIVE_CASES:
        check("live-paraphrase", text, "live", "matched", expect_id=qid)

    for text in UNSUPPORTED_CASES:
        check("unsupported-in-demo", text, "demo", "unsupported")
        check("unsupported-in-live", text, "live", "unsupported")

    for text, typed_mode, true_mode in CROSS_MODE_CASES:
        check("cross-mode", text, typed_mode, "wrong_mode", expect_other_mode=true_mode)

    print(f"Threshold: {router.MATCH_THRESHOLD}")
    print(f"Total cases: {results['total']}")
    print(f"Correct-match rate: {results['correct']}/{results['total']} = {100*results['correct']/results['total']:.1f}%")
    print(f"False-accept count: {results['false_accept']}")
    print(f"False-reject count: {results['false_reject']}")
    print(f"Cross-mode leak count: {results['cross_mode_leak']}")
    print()
    if failures:
        print(f"=== {len(failures)} FAILURES ===")
        for f in failures:
            print(f"[{f['label']}] mode={f['mode']!r} text={f['typed_text']!r}")
            print(f"    expected={f['expected']}  got={f['got']}")
    else:
        print("No failures.")

    return results, failures


if __name__ == "__main__":
    run()
