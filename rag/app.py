"""
Streamlit UI for the Nestasia competitive-intelligence system.

Primary way to ask a question is a DROPDOWN (not free text) -- the 25 demo
questions grouped by category, or the 9 live questions ungrouped. A plain
text box below it is a secondary, explicitly "(experimental)" path that
routes through router.py; it only ever answers one of the same fixed
questions (or says it can't), never free-generates a new answer.

Run:
    streamlit run app.py
"""
import datetime
import json
import os

import pandas as pd
import streamlit as st

import presenters as pr
import question_registry as qr
import router
from generate import check_ollama
from retrieve import retrieve

LOG_FILE = os.path.join(os.path.dirname(__file__), ".interaction_log.jsonl")

DEMO_CATEGORY_ORDER = ["Product", "Price", "Place", "Promotion", "Cross-cutting"]
PLACEHOLDER = "-- Select a question --"

# live_6 (stock_mismatch_aggregate) is fully templated in generate.py --
# no LLM call. live_8 (unsupported_internal_data) is a refusal computed
# entirely by retrieve.py -- also no LLM call needed for it, even though
# generate()'s current dispatch would otherwise route it through Ollama;
# we bypass generate() for both and build Details straight from retrieve().
NO_LLM_LIVE_IDS = {"live_6", "live_8", "live_9"}

_FULLY_VERIFIED_FALLBACK = "This answer uses fully verified, complete data."
_NO_CAVEAT_FALLBACK = "No further caveat applies."

_WHAT_THIS_IS_TEXT = (
    "This answers a fixed set of built-in business questions -- it does not free-generate answers "
    "to anything else. Demo mode shows what this tool looks like once Nestasia connects real sales, "
    "inventory, and channel data (the numbers are synthetic placeholders). Live mode only uses data "
    "actually collected so far -- catalogue, prices, stock display, and ad creative."
)

MODE_BANNERS = {
    "demo": ("amber", "DEMO -- synthetic data, not Nestasia's real business figures. "
                        "Shows how the system works once sales, inventory and channel data are connected."),
    "live": ("green", "LIVE -- real collected data (catalogue, prices, stock display, ad creative). "
                       "Sales, margin and channel-performance data are not connected."),
}
_BANNER_CSS = {
    "amber": "background-color:#F3E3CE; color:#6B4A2B; border:1px solid #DEB887;",
    "green": "background-color:#D9E7D3; color:#2F4A2A; border:1px solid #A9C7A0;",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _log_interaction(mode, typed_text, matched_id, confidence, outcome):
    """Local JSONL only, never a database. Logging must never be able to
    crash the app, so failures here are swallowed."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "mode": mode, "typed_text": typed_text, "matched_id": matched_id,
                "confidence": confidence, "outcome": outcome,
            }) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Chip label helpers
# ---------------------------------------------------------------------------
def _data_chip(sources):
    kinds = {s["kind"] for s in sources}
    if not kinds:
        return "Demo"  # no sources at all only happens for a pure refusal; treated as non-real
    if kinds == {"real"}:
        return "Real"
    if kinds == {"demo"}:
        return "Demo"
    return "Mixed"


_COVERAGE_LABELS = {"Full": "Complete", "Partial": "Partial", "Not supported": "Not supported"}


def _coverage_chip(scope_label):
    return _COVERAGE_LABELS.get(scope_label, scope_label)


def _grouped_demo_questions():
    grouped = {}
    for e in qr.all_for_mode("demo"):
        grouped.setdefault(e["category"], []).append(e)
    return grouped


# ---------------------------------------------------------------------------
# Card builders -- return plain dicts (no Streamlit calls), so AppTest-based
# tests can build/inspect cards without rendering anything.
# ---------------------------------------------------------------------------
def build_demo_card(entry, typed_text, confidence, match_note):
    result = qr.call_handler(entry, typed_text=typed_text)
    raw = result["raw_result"]
    bundle = pr.PRESENTERS[entry["id"]](raw)
    data_chip = _data_chip(bundle["sources"])
    coverage_chip = _coverage_chip(result["scope_label"])
    disclaimer = bundle["caveat"] or (
        _FULLY_VERIFIED_FALLBACK if (data_chip == "Real" and coverage_chip == "Complete") else _NO_CAVEAT_FALLBACK
    )
    return {
        "outcome": "matched",
        "mode": "demo",
        "match_note": match_note,
        "question_text": entry["text"],
        "headline": bundle["headline"],
        "visual": bundle["visual"],
        "suggestion": bundle["suggestion"],
        "disclaimer": disclaimer,
        "firm_up": bundle["firm_up"],
        "sources": bundle["sources"],
        "data_chip": data_chip,
        "coverage_chip": coverage_chip,
        "coverage_missing": result["scope_missing"],
        "unverified": False,
        "unverified_reason": None,
        "details": {
            "matched_id": entry["id"],
            "confidence": confidence,
            "columns": [c.get("label") if isinstance(c, dict) else c for c in bundle["visual"]["columns"]]
                       if bundle["visual"] and bundle["visual"]["type"] == "table" else None,
            "raw_answer": result["body"],
        },
    }


def build_live_card(entry, typed_text, confidence, match_note):
    bundle = pr.PRESENTERS[entry["id"]](typed_text)
    if entry["id"] in NO_LLM_LIVE_IDS:
        evidence_result = retrieve(typed_text, db_mode="production")
        raw_answer = json.dumps(evidence_result, indent=2, default=str)
        unverified, unverified_reason = False, None
    else:
        result = qr.call_handler(entry, typed_text=typed_text)
        raw_answer = result["answer"]
        unverified = not result["safety_check_passed"]
        unverified_reason = result.get("safety_check_reason")

    data_chip = _data_chip(bundle["sources"])
    coverage_chip = _coverage_chip(entry["scope"])
    disclaimer = bundle["caveat"] or (
        _FULLY_VERIFIED_FALLBACK if (data_chip == "Real" and coverage_chip == "Complete") else _NO_CAVEAT_FALLBACK
    )
    coverage_missing = entry["notes"] or ("Nothing missing." if entry["scope"] == "Full" else "Missing: not specified.")
    return {
        "outcome": "matched",
        "mode": "live",
        "match_note": match_note,
        "question_text": entry["text"],
        "headline": bundle["headline"],
        "visual": bundle["visual"],
        "suggestion": bundle["suggestion"],
        "disclaimer": disclaimer,
        "firm_up": bundle["firm_up"],
        "sources": bundle["sources"],
        "data_chip": data_chip,
        "coverage_chip": coverage_chip,
        "coverage_missing": coverage_missing,
        "unverified": unverified,
        "unverified_reason": unverified_reason,
        "details": {
            "matched_id": entry["id"],
            "confidence": confidence,
            "columns": [c.get("label") if isinstance(c, dict) else c for c in bundle["visual"]["columns"]]
                       if bundle["visual"] and bundle["visual"]["type"] == "table" else None,
            "raw_answer": raw_answer,
        },
    }


def build_unsupported_card(mode, typed_text):
    if mode == "demo":
        grouped = _grouped_demo_questions()
    else:
        grouped = {"Live questions": qr.all_for_mode("live")}
    return {
        "outcome": "unsupported",
        "mode": mode,
        "typed_text": typed_text,
        "grouped_questions": grouped,
    }


def build_ollama_down_card(mode, typed_text, entry):
    return {
        "outcome": "ollama_down",
        "mode": mode,
        "typed_text": typed_text,
        "question_text": entry["text"],
    }


def process_question(typed_text, mode, entry_id=None):
    """entry_id set -> came from a dropdown (exact, no routing ambiguity,
    no match_note shown). entry_id None -> came from the free-text box,
    routed through router.py; the match (or lack of one) is logged either
    way."""
    if entry_id is not None:
        entry = qr.get(entry_id)
        outcome = "matched"
        confidence = 1.0
        match_note = None
    else:
        route_result = router.route(typed_text, mode)
        outcome = route_result["outcome"]
        _log_interaction(mode, typed_text, route_result.get("question_id"), route_result.get("confidence"), outcome)
        if outcome == "matched":
            entry = qr.get(route_result["question_id"])
            confidence = route_result["confidence"]
            match_note = f"Matched: \"{entry['text']}\" ({round(confidence * 100)}% confidence)"
        elif outcome == "wrong_mode":
            st.session_state.current_card = {
                "outcome": "wrong_mode", "mode": mode, "typed_text": typed_text,
                "which_mode_answers_it": route_result["which_mode_answers_it"],
            }
            return
        else:
            st.session_state.current_card = build_unsupported_card(mode, typed_text)
            return

    if entry_id is not None:
        _log_interaction(mode, typed_text, entry_id, confidence, outcome)

    if mode == "demo":
        card = build_demo_card(entry, typed_text, confidence, match_note)
    else:
        if entry["id"] in NO_LLM_LIVE_IDS:
            card = build_live_card(entry, typed_text, confidence, match_note)
        else:
            ok, _msg = check_ollama()
            if not ok:
                card = build_ollama_down_card(mode, typed_text, entry)
            else:
                card = build_live_card(entry, typed_text, confidence, match_note)

    st.session_state.current_card = card


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


def _chip(label, value, color):
    return (f'<span style="background-color:{color}; padding:3px 10px; border-radius:12px; '
            f'font-size:0.8em; font-weight:600; margin-right:6px;">{label}: {value}</span>')


_TABLE_ROW_PX = 35
_TABLE_HEADER_PX = 38
_TABLE_MAX_PX = 320


def render_visual(visual):
    if not visual:
        return
    if visual["type"] == "table":
        labels = [c["label"] if isinstance(c, dict) else c for c in visual["columns"]]
        df = pd.DataFrame(visual["rows"], columns=labels)
        # Fixed-height, scrollable rather than letting a long table (e.g.
        # Q6's ~76 rows) push everything below it off-screen -- height
        # shrinks to fit short tables instead of padding them with empty
        # space, but never grows past _TABLE_MAX_PX.
        height = min(_TABLE_MAX_PX, _TABLE_HEADER_PX + _TABLE_ROW_PX * max(len(df), 1))
        st.dataframe(df, width="stretch", hide_index=True, height=height)
    elif visual["type"] == "bar":
        df = pd.DataFrame({"value": visual["values"]}, index=visual["labels"])
        st.bar_chart(df, width="stretch")
        if visual.get("unit"):
            st.caption(f"Unit: {visual['unit']}")


def render_sources(sources):
    st.markdown("**Sources**")
    if not sources:
        st.markdown("- demo data")
        return
    for s in sources:
        if s.get("url"):
            st.markdown(f"- [{s['label']}]({s['url']})")
        elif s["kind"] == "demo":
            st.markdown("- demo data")
        else:
            st.markdown(f"- {s['label']}")


def render_card(card):
    if card["outcome"] == "matched":
        color, _ = MODE_BANNERS[card["mode"]]
        chips = (
            _chip("Mode", card["mode"].upper(), _BANNER_CSS[color].split(";")[0].split(":")[1]) +
            _chip("Data", card["data_chip"], "#EADFC8") +
            _chip("Coverage", card["coverage_chip"], "#EADFC8")
        )
        st.markdown(chips, unsafe_allow_html=True)
        # coverage_missing already reads as a complete sentence on its own
        # ("Nothing missing.", "Missing: ...", etc.) -- no extra prefix here.
        st.caption(card["coverage_missing"])

        if card.get("match_note"):
            st.caption(card["match_note"])

        if card.get("unverified"):
            st.warning(f"UNVERIFIED -- needs human review: {card.get('unverified_reason')}")

        st.markdown(f"### {card['headline']}")
        render_visual(card["visual"])
        st.caption(card["suggestion"])

        st.markdown(
            f'<div style="background-color:#FBEAEA; border:1px solid #E8B4B4; border-radius:6px; '
            f'padding:8px 12px; margin:8px 0;"><b>Read before relying on this:</b> {card["disclaimer"]}</div>',
            unsafe_allow_html=True,
        )

        if card["firm_up"] and card["firm_up"] != "N/A":
            st.caption(f"What would confirm this: {card['firm_up']}")

        render_sources(card["sources"])

        with st.expander("Details"):
            d = card["details"]
            st.write(f"Matched question id: `{d['matched_id']}` (confidence: {round(d['confidence'] * 100)}%)")
            if d["columns"]:
                st.write(f"Table columns: {', '.join(d['columns'])}")
            st.text(d["raw_answer"])

        st.caption(f"{'DEMO' if card['mode'] == 'demo' else 'LIVE'} card -- "
                    f"{'synthetic demo data' if card['mode'] == 'demo' else 'real collected data'}.")

    elif card["outcome"] == "unsupported":
        st.write(f"**Unsupported in {card['mode'].upper()} mode**: \"{card['typed_text']}\" doesn't match anything "
                 f"this system can answer in this mode. Here's what IS answerable:")
        for cat, entries in card["grouped_questions"].items():
            st.markdown(f"**{cat}**")
            for e in entries:
                st.markdown(f"- {e['text']}")

    elif card["outcome"] == "wrong_mode":
        st.write(f"**That's answered in {card['which_mode_answers_it'].upper()} mode, not "
                 f"{card['mode'].upper()} mode.** Switch modes in the sidebar to ask it.")

    elif card["outcome"] == "ollama_down":
        st.error("Live AI-generated answers need Ollama running locally -- start it and refresh.")


# ---------------------------------------------------------------------------
# Sidebar (mode toggle, dropdown, free-text box, "what this is")
# ---------------------------------------------------------------------------
def render_sidebar(mode):
    if mode == "demo":
        grouped = _grouped_demo_questions()
        options = [PLACEHOLDER]
        option_ids = {}
        for cat in DEMO_CATEGORY_ORDER:
            for e in grouped.get(cat, []):
                label = f"[{cat}] {e['text']}"
                options.append(label)
                option_ids[label] = e["id"]
    else:
        options = [PLACEHOLDER]
        option_ids = {}
        for e in qr.all_for_mode("live"):
            options.append(e["text"])
            option_ids[e["text"]] = e["id"]

    choice = st.sidebar.selectbox("Ask a question", options, key=f"dropdown_{mode}")
    if choice != PLACEHOLDER:
        entry_id = option_ids[choice]
        if st.session_state.get("last_processed") != (mode, entry_id, "dropdown"):
            st.session_state.last_processed = (mode, entry_id, "dropdown")
            process_question(choice, mode, entry_id=entry_id)

    st.sidebar.caption("Or ask something else (experimental)")
    typed = st.sidebar.text_input("Free text", key=f"freetext_{mode}", label_visibility="collapsed",
                                   placeholder="Type a question...")
    if st.sidebar.button("Ask", key=f"freetext_btn_{mode}"):
        if typed and typed.strip():
            process_question(typed.strip(), mode, entry_id=None)

    st.sidebar.markdown("---")
    st.sidebar.subheader("What this is")
    st.sidebar.caption(_WHAT_THIS_IS_TEXT)


def main():
    st.set_page_config(page_title="Nestasia Competitive Intelligence", layout="wide")

    if "mode" not in st.session_state:
        st.session_state.mode = "demo"
    if "current_card" not in st.session_state:
        st.session_state.current_card = None

    new_mode = st.sidebar.radio(
        "Mode", options=["live", "demo"], index=1 if st.session_state.mode == "demo" else 0,
        format_func=lambda m: "Live (real data)" if m == "live" else "Demo (synthetic data)",
    )
    if new_mode != st.session_state.mode:
        # Switching modes clears the shown answer -- a DEMO card left on
        # screen under a LIVE banner (or vice versa) reads as if the new
        # mode produced it, which it didn't.
        st.session_state.current_card = None
    st.session_state.mode = new_mode
    render_sidebar(st.session_state.mode)

    # h2-sized, not st.title()'s large h1 -- the page title was dominating
    # the first screenful of vertical space above the fold.
    st.markdown('<h2 style="margin-bottom:0.4em;">Nestasia Competitive Intelligence</h2>', unsafe_allow_html=True)
    render_mode_banner(st.session_state.mode)

    if st.session_state.current_card:
        render_card(st.session_state.current_card)
    else:
        st.info("Pick a question from the sidebar to get started.")


if __name__ == "__main__":
    main()
