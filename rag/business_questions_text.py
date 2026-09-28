"""
Question text for the 25 gold-set questions in business_questions_25.md,
keyed by number, for use by generate.py's fixture-question templating.
Kept as a small Python literal (not parsed from the .md at import time) so
this module has no file-path dependency; the numbers/text below were
copied verbatim from business_questions_25.md and must be kept in sync
with it by hand if that file ever changes.
"""

BUSINESS_QUESTIONS_25 = {
    1: "Which of our products should we prioritize promoting this quarter based on sales momentum and stock health?",
    2: "Are we sitting on slow-moving inventory that's tying up cash right now?",
    3: "Which product category is our strongest performer relative to competitors?",
    4: "Do we have bestsellers at risk of going out of stock soon?",
    5: "Which recent launches are underperforming and might need repositioning?",
    6: "Are we leaving margin on the table by underpricing versus competitors anywhere?",
    7: "Which products have the best margin-to-volume ratio right now?",
    8: "Should we run a promotion on a specific category to clear inventory?",
    9: "How does our profitability in Cookware compare to Bakeware?",
    10: "Are any SKUs priced in a way that's hurting conversion?",
    11: "Which sales channel is driving the most revenue for us right now?",
    12: "Are we out of stock on a high-demand product on any specific channel?",
    13: "Should we expand our qCommerce assortment based on how it's performing?",
    14: "Which channel has our highest return/complaint rate?",
    15: "Where are competitors outperforming us in availability?",
    16: "Which ad campaign is actually driving sales, not just impressions?",
    17: "Should we increase ad spend on a specific product given current trends?",
    18: "Are festive/discount campaigns outperforming everyday pricing?",
    19: "What does social engagement tell us about what to promote next?",
    20: "Which competitor promotional approach looks most effective right now?",
    21: "Which single product should we prioritize across marketing, inventory, and pricing this month?",
    22: "If we could fix only one thing in Kitchen this quarter, what and why?",
    23: "Which product is quietly becoming a problem before it's obvious?",
    24: "Where are we most vulnerable to a competitor taking share right now?",
    25: "If we had to cut 10% of our SKUs, which should go?",
}
