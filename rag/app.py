"""
Streamlit UI for the Nestasia competitive-intelligence system. Routes a
typed question through router.py against the active mode's registry
(question_registry.py) and renders one self-contained card per answer --
mode label, accuracy/scope badges, citation, and disclaimers all inline
and visible by default (not in a collapsed expander), because a single
card is what someone actually screenshots and shares.

Run:
    streamlit run app.py
"""
import datetime
import json
import os

import streamlit as st

import question_registry as qr
import router
from generate import check_ollama

LOG_FILE = os.path.join(os.path.dirname(__file__), ".interaction_log.jsonl")

CATEGORY_ORDER = ["Product", "Price", "Place", "Promotion", "Cross-cutting", "Real-data"]

# Real/production tables each live intent draws from -- retrieve.py's
# evidence rows don't carry a literal "table" field (they carry
# source_type/collected_at per row instead), so this is the one place that
# names which table(s) back each intent's numbers, for the citation line.
INTENT_TABLES = {
    "price_comparison": ["sku", "price_history"],
    "stock_mismatch_lookup": ["sku"],
    "stock_mismatch_aggregate": ["sku"],
    "sku_count_by_category": ["sku"],
    "completeness_check": ["sku"],
    "ad_theme_lookup": ["paid_ad_creative"],
    "unsupported_internal_data": [],
    "unknown": [],
}

MODE_BANNERS = {
    "demo": ("amber", "DEMO -- synthetic data, not Nestasia's real business figures. "
                        "Shows how the system works once sales, inventory and channel data are connected."),
    "live": ("green", "LIVE -- real collected data (catalogue, prices, stock display, ad creative). "
                       "Sales, margin and channel-performance data are not connected."),
}

_BANNER_CSS = {
    "amber": "background-color:#fff3cd; color:#664d03; border:1px solid #ffe69c;",
    "green": "background-color:#d1e7dd; color:#0f5132; border:1px solid #a3cfbb;",
}


def _log_interaction(mode, typed_text, matched_id, confidence, outcome):
    """Local JSONL only, never a database, per instructions -- purely for
    later review of how people actually phrase questions. Logging must
    never be able to crash the app, so failures here are swallowed."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "mode": mode, "typed_text": typed_text, "matched_id": matched_id,
                "confidence": confidence, "outcome": outcome,
            }) + "\n")
    except Exception:
        pass


def _walk_collect(evidence, keys):
    found = set()

    def rec(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in keys and v:
                    if isinstance(v, (list, set, tuple)):
                        found.update(str(i) for i in v if i)
                    else:
                        found.add(str(v))
                rec(v)
        elif isinstance(x, list):
            for i in x:
                rec(i)

    rec(evidence)
    return found


def _grouped_questions(mode):
    grouped = {}
    for e in qr.all_for_mode(mode):
        grouped.setdefault(e["category"], []).append(e)
    return grouped


# ---------------------------------------------------------------------------
# Card builders -- each returns a plain dict (no Streamlit calls), so the
# AppTest-based tests can build/inspect cards without rendering anything.
# ---------------------------------------------------------------------------
def build_demo_card(entry, typed_text, confidence, alternatives):
    result = qr.call_handler(entry, typed_text=typed_text)
    raw = result["raw_result"]
    special_notes = [n for n in (raw.get("special_note"),) if n]
    return {
        "outcome": "matched",
        "mode": "demo",
        "typed_text": typed_text,
        "matched_id": entry["id"],
        "match_confidence": confidence,
        "alternatives": alternatives,
        "question_text": entry["text"],
        "answer_body": result["body"],
        "accuracy": result["accuracy_label"],
        "scope": result["scope_label"],
        "scope_missing": result["scope_missing"],
        "citation": result["citation"],
        "disclaimers": result["disclaimers"],
        "special_notes": special_notes,
        "unverified": False,
        "unverified_reason": None,
    }


def build_live_card(entry, typed_text, confidence, alternatives):
    result = qr.call_handler(entry, typed_text=typed_text)
    intent = result["intent"]
    tables = INTENT_TABLES.get(intent, [])
    source_types = sorted(_walk_collect(result["evidence"], {"source_type", "source_types"}))
    collected_ats = sorted(_walk_collect(result["evidence"], {"collected_at", "latest_collected_at"}), reverse=True)

    if tables:
        parts = [f"tables: {', '.join(tables)}", f"source_type: {', '.join(source_types) or 'n/a'}"]
        if collected_ats:
            parts.append(f"collected_at (latest): {collected_ats[0]}")
        citation = "Citation: " + "; ".join(parts)
        accuracy = "Verified"
    else:
        citation = "Citation: none -- no supporting table for this question."
        accuracy = "N/A -- unsupported by this schema"

    if tables:
        disclaimers = ["This is real, live-collected data (not synthetic)."]
    else:
        disclaimers = ["No underlying table backs this answer -- this question is structurally "
                        "unsupported by the live schema (see the answer text for why)."]

    return {
        "outcome": "matched",
        "mode": "live",
        "typed_text": typed_text,
        "matched_id": entry["id"],
        "match_confidence": confidence,
        "alternatives": alternatives,
        "question_text": entry["text"],
        "answer_body": result["answer"],
        "accuracy": accuracy,
        "scope": entry["scope"],
        "scope_missing": entry["notes"] or ("Nothing missing." if entry["scope"] == "Full" else "See notes."),
        "citation": citation,
        "disclaimers": disclaimers,
        "special_notes": [],
        "unverified": not result["safety_check_passed"],
        "unverified_reason": result.get("safety_check_reason"),
    }


def build_unsupported_card(mode, typed_text, nearest):
    return {
        "outcome": "unsupported",
        "mode": mode,
        "typed_text": typed_text,
        "nearest": nearest,
        "grouped_questions": _grouped_questions(mode),
    }


def build_wrong_mode_card(mode, typed_text, which_mode, other_question_id):
    return {
        "outcome": "wrong_mode",
        "mode": mode,
        "typed_text": typed_text,
        "which_mode_answers_it": which_mode,
        "other_question_id": other_question_id,
        "other_question_text": qr.get(other_question_id)["text"],
    }


def build_ollama_down_card(mode, typed_text, entry):
    return {
        "outcome": "ollama_down",
        "mode": mode,
        "typed_text": typed_text,
        "question_text": entry["text"],
        "matched_id": entry["id"],
    }


def process_question(typed_text, mode, forced_question_id=None):
    """The single place that turns typed text (or a sidebar/alternative
    click, via forced_question_id) into a card and appends it to history.
    Never renders anything itself -- render_card() does that separately,
    which is what makes this testable via AppTest without a browser."""
    if forced_question_id is not None:
        entry = qr.get(forced_question_id)
        route_result = {"outcome": "matched", "question_id": forced_question_id, "confidence": 1.0, "top_alternatives": []}
    else:
        route_result = router.route(typed_text, mode)

    outcome = route_result["outcome"]
    _log_interaction(mode, typed_text, route_result.get("question_id"), route_result.get("confidence"), outcome)

    if outcome == "matched":
        entry = qr.get(route_result["question_id"])
        alt_texts = [{"question_id": a["question_id"], "text": qr.get(a["question_id"])["text"],
                      "confidence": a["confidence"]} for a in route_result.get("top_alternatives", [])]
        if mode == "demo":
            card = build_demo_card(entry, typed_text, route_result["confidence"], alt_texts)
        else:
            ok, _msg = check_ollama()
            if not ok:
                card = build_ollama_down_card(mode, typed_text, entry)
            else:
                card = build_live_card(entry, typed_text, route_result["confidence"], alt_texts)
    elif outcome == "wrong_mode":
        card = build_wrong_mode_card(mode, typed_text, route_result["which_mode_answers_it"], route_result["question_id"])
    else:
        card = build_unsupported_card(mode, typed_text, route_result["nearest_questions"])

    st.session_state.history.append(card)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_mode_banner(mode):
    color, text = MODE_BANNERS[mode]
    st.markdown(
        f'<div style="{_BANNER_CSS[color]} padding:10px 16px; border-radius:6px; '
        f'font-weight:600; margin-bottom:12px;">{text}</div>',
        unsafe_allow_html=True,
    )


def render_card(card, index):
    color, _ = MODE_BANNERS[card["mode"]]
    st.markdown(
        f'<span style="{_BANNER_CSS[color]} padding:2px 8px; border-radius:4px; '
        f'font-size:0.8em; font-weight:600;">{card["mode"].upper()} MODE</span>',
        unsafe_allow_html=True,
    )

    if card["outcome"] == "matched":
        st.caption(f"I understood this as: **{card['question_text']}** (match {round(card['match_confidence']*100)}%)")
        if card["alternatives"]:
            with st.popover("Not what I meant?"):
                for alt in card["alternatives"]:
                    if st.button(f"{alt['text']} ({round(alt['confidence']*100)}%)", key=f"alt_{index}_{alt['question_id']}"):
                        process_question(alt["text"], card["mode"], forced_question_id=alt["question_id"])

        if card.get("unverified"):
            st.warning(f"UNVERIFIED -- needs human review: {card.get('unverified_reason')}")

        st.markdown(card["answer_body"])

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Accuracy", card["accuracy"])
        with col2:
            st.metric("Scope", card["scope"])
        st.caption(card["scope_missing"])

        st.info(card["citation"])
        for d in card["disclaimers"]:
            st.warning(d)
        for n in card["special_notes"]:
            st.error(n)

    elif card["outcome"] == "unsupported":
        st.write(f"**Unsupported in {card['mode'].upper()} mode**: \"{card['typed_text']}\" doesn't match anything "
                 f"this system can answer in this mode. Here's what IS answerable, by category:")
        for cat in CATEGORY_ORDER:
            entries = card["grouped_questions"].get(cat)
            if not entries:
                continue
            st.markdown(f"**{cat}**")
            for e in entries:
                st.markdown(f"- {e['text']}")

    elif card["outcome"] == "wrong_mode":
        st.write(f"**That's answered in {card['which_mode_answers_it'].upper()} mode, not {card['mode'].upper()} mode.** "
                 f"Closest match there: \"{card['other_question_text']}\". Switch modes in the sidebar to ask it.")

    elif card["outcome"] == "ollama_down":
        st.error(f"This question (\"{card['question_text']}\") needs the local Ollama model to generate an answer, "
                 f"and Ollama isn't reachable right now. Start Ollama and try again -- Demo mode doesn't need it "
                 f"and works without it.")

    st.divider()


def render_sidebar(mode):
    st.sidebar.header("Answerable questions")
    grouped = _grouped_questions(mode)
    for cat in CATEGORY_ORDER:
        entries = grouped.get(cat)
        if not entries:
            continue
        with st.sidebar.expander(cat, expanded=False):
            for e in entries:
                if st.button(e["text"], key=f"sidebar_{e['id']}"):
                    process_question(e["text"], mode, forced_question_id=e["id"])

    st.sidebar.markdown("---")
    st.sidebar.subheader("What this demo is and isn't")
    st.sidebar.caption(
        "In DEMO mode the sales/inventory/channel numbers are generated, not real. "
        "Answers like \"candidates to prioritise\" show what the OUTPUT SHAPE would look "
        "like once real data is connected -- they are not instructions to act on today. "
        "LIVE mode is real collected data, but only catalogue/price/stock-display/ad data; "
        "it has no sales, margin, or channel-performance numbers at all."
    )


def main():
    st.set_page_config(page_title="Nestasia Competitive Intelligence", layout="wide")

    if "history" not in st.session_state:
        st.session_state.history = []
    if "mode" not in st.session_state:
        st.session_state.mode = "demo"

    st.session_state.mode = st.sidebar.radio(
        "Mode", options=["demo", "live"], index=0 if st.session_state.mode == "demo" else 1,
        format_func=lambda m: "Demo (synthetic)" if m == "demo" else "Live (real data)",
    )
    render_sidebar(st.session_state.mode)

    st.title("Nestasia Competitive Intelligence")
    render_mode_banner(st.session_state.mode)

    # chat_input is captured here, BEFORE the history render loop below, so
    # a newly typed question's card renders in the same script pass instead
    # of needing an explicit st.rerun() (Streamlit auto-pins chat_input to
    # the bottom of the page regardless of where it's called in the code).
    typed = st.chat_input("Ask a question...")
    if typed:
        process_question(typed, st.session_state.mode)

    for i, card in enumerate(st.session_state.history):
        render_card(card, i)


if __name__ == "__main__":
    main()
