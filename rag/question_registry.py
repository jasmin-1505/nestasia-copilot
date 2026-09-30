"""
Single registry of every question this system can answer -- the 25 fixture
demo questions (business_questions_25.md) and the 8 original live/
production questions (test_questions.py). router.py and any future UI
route against this registry ONLY; adding a 26th question means writing a
handler function and adding one entry here, with NO changes to router.py
or retrieve.py/generate.py's dispatch logic.

Each entry:
  id            -- unique string, "demo_<n>" or "live_<n>"
  text           -- the canonical question text (used to build/cache the
                     embedding for matching, and as ground truth wording)
  category       -- "Product" | "Price" | "Place" | "Promotion" |
                     "Cross-cutting" | "Real-data"
  mode           -- "demo" (fixture) | "live" (production)
  handler        -- the actual callable to invoke
  arg_style      -- how call_handler() invokes `handler`:
                       "question_number" -> handler(entry["question_number"])
                       "typed_text"      -> handler(typed_text)  (the
                            user's ACTUAL typed text, not entry["text"] --
                            live questions extract brand/category entities
                            from the literal wording via classify_intent(),
                            so the real input must be passed through, not
                            the registry's canonical phrasing)
  question_number -- only for arg_style="question_number"
  scope          -- a snapshot label (Full/Partial/Not supported) as of
                     this build, for router/UI hints; the authoritative,
                     live value is always whatever the handler itself
                     returns when actually called
  notes          -- free-text, e.g. why scope is what it is

Adding a new demo question: write its handler in
fixture_business_queries.py (returning the standard supported/data/
sources/gap_explanation/special_note shape used by all 25), add one
generate_fixture_business_answer-style entry with arg_style=
"question_number". Adding a new live question: no new handler is needed at
all if it fits an existing retrieve.py intent -- just add an entry with
arg_style="typed_text" pointing at generate(). Neither case touches
router.py.
"""
from generate import generate, generate_fixture_business_answer

_DEMO_CATEGORY_BY_RANGE = [
    (range(1, 6), "Product"),
    (range(6, 11), "Price"),
    (range(11, 16), "Place"),
    (range(16, 21), "Promotion"),
    (range(21, 26), "Cross-cutting"),
]


def _demo_category(n):
    for r, cat in _DEMO_CATEGORY_BY_RANGE:
        if n in r:
            return cat
    raise ValueError(f"question_number={n} out of the registered 1-25 range")


# Snapshot scope labels as of the last full run (see the Step 5 report) --
# router/UI hints only, not re-derived here.
_DEMO_SCOPE = {
    1: "Full", 2: "Full", 3: "Full", 4: "Full", 5: "Full", 6: "Partial", 7: "Full",
    8: "Full", 9: "Full", 10: "Partial", 11: "Full", 12: "Full", 13: "Full", 14: "Full",
    15: "Full", 16: "Partial", 17: "Partial", 18: "Partial", 19: "Partial", 20: "Partial",
    21: "Partial", 22: "Full", 23: "Full", 24: "Full", 25: "Full",
}

_DEMO_TEXT = {
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

_DEMO_NOTES = {
    6: "Competitor margin is a synthetic placeholder, never a real fact -- see the handler's caveat.",
    10: "Downgraded to Partial: the conversion ratio is confounded with the generator's own price/discount-to-volume rule (circularity).",
    16: "The ad-to-product attribution is fabricated by construction, not recovered.",
    17: "Same fabricated-attribution gap as Q16; decision-framed, not a directive.",
    18: "Circular by construction: synthetic sales were generated from each SKU's discount level.",
    19: "Social engagement is brand-level only, with no product/theme link.",
    20: "Reports ad activity (real, Verified), not effectiveness -- Scope stays Partial even though Accuracy is Verified.",
    21: "The marketing leg rests on the same fabricated ad-to-product attribution as Q16/Q17.",
}


def _demo_entry(n):
    return {
        "id": f"demo_{n}",
        "text": _DEMO_TEXT[n],
        "category": _demo_category(n),
        "mode": "demo",
        "handler": generate_fixture_business_answer,
        "arg_style": "question_number",
        "question_number": n,
        "scope": _DEMO_SCOPE[n],
        "notes": _DEMO_NOTES.get(n, ""),
    }


# The 8 original live/production questions (test_questions.py), each
# routed through generate() -- which re-runs retrieve.py's classify_intent
# on the ACTUAL typed text to extract brand/category entities, so a
# paraphrase naming a different brand still resolves correctly.
_LIVE_QUESTIONS = [
    ("How does our Cookware pricing compare to Home Centre's?", "Partial",
     "Brand and category are parsed from the question text. Downgraded from Full: Nestasia's "
     "Cookware collection is confirmed under-collected (the site lists 88 products, only 12 are "
     "on file) as of 2026-09-27 -- the 12 SKUs priced here are real but not the whole category."),
    ("Which of our products show a stock-display inconsistency?", "Partial",
     "Looks at Nestasia's own listings; some categories (Container, Lunch Boxes+Bags) are still incomplete."),
    ("Does Milton have the same stock-display bug we do?", "Full",
     "Looks at Milton's listings; complete data, confirmed bug."),
    ("Does Home Centre have the stock-display bug?", "Not supported",
     "Cannot be tested -- Home Centre's site architecture gives no on-page signal to check at all."),
    ("Does Borosil have this bug too?", "Full",
     "Looks at Borosil's listings; complete data, confirmed bug."),
    ("How many of our tracked competitors have this stock-display bug?", "Partial",
     "Tallies all tracked competitors; Home Centre is untestable and must be reported separately, not folded into either count."),
    ("Which Container collections have incomplete data right now?", "Partial",
     "Checks collection completeness by category; the answer itself IS that the data is incomplete."),
    ("What's our best-selling SKU in Cookware?", "Not supported",
     "No sales table exists in the live/production schema at all."),
]


def _live_entry(i, text, scope, notes):
    return {
        "id": f"live_{i}",
        "text": text,
        "category": "Real-data",
        "mode": "live",
        "handler": generate,
        "arg_style": "typed_text",
        "scope": scope,
        "notes": notes,
    }


REGISTRY = (
    [_demo_entry(n) for n in range(1, 26)] +
    [_live_entry(i, text, scope, notes) for i, (text, scope, notes) in enumerate(_LIVE_QUESTIONS, start=1)]
)

_BY_ID = {e["id"]: e for e in REGISTRY}


def get(question_id):
    return _BY_ID[question_id]


def all_for_mode(mode):
    if mode not in ("demo", "live"):
        raise ValueError(f"mode must be 'demo' or 'live', got {mode!r}")
    return [e for e in REGISTRY if e["mode"] == mode]


def call_handler(entry, typed_text=None):
    """The ONE place that knows how to invoke a registry entry's handler.
    router.py calls this and never inspects an entry's handler/arg_style
    itself -- so a new arg_style can be added here later without touching
    router.py."""
    if entry["arg_style"] == "question_number":
        return entry["handler"](entry["question_number"])
    if entry["arg_style"] == "typed_text":
        if typed_text is None:
            raise ValueError(f"entry {entry['id']!r} requires typed_text (arg_style='typed_text')")
        return entry["handler"](typed_text)
    raise ValueError(f"Unknown arg_style {entry['arg_style']!r} on entry {entry['id']!r}")
