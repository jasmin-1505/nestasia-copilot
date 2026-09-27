"""
Generation layer: takes retrieve.py's evidence for a question and calls a
local Ollama model to produce a grounded, cited answer. No cloud API, no
API key -- Ollama's HTTP API on localhost:11434.

Citation-or-silence discipline, enforced in the system prompt sent to the
model (not just hoped for):
  1. Only state facts present in the retrieved evidence. Cite source_type +
     collected_at for every factual claim. If the evidence doesn't cover
     something, say "we don't have this data" -- never guess or fall back
     on general knowledge about these brands.
  2. Respect collection_complete: if any evidence group has
     collection_complete=False (or all_complete=false / any_partial=true),
     the answer must flag that explicitly as partial, not silently treat it
     as a full picture.
  3. Respect the four-value stock_mismatch field exactly. true/false/"N/A"/
     "Unknown" are not interchangeable -- retrieve.py's classification
     layer has ALREADY computed brand_testable_for_mismatch /
     brand_has_confirmed_mismatch / brand_confirmed_clean per brand
     specifically so the model doesn't have to (and can't get wrong by)
     averaging N/A or Unknown into a false "clean" reading.
  4. Refuse anything requiring internal data (sales, margin, revenue) --
     retrieve.py already short-circuits these to zero evidence with an
     explicit reason; the prompt reinforces refusing outright rather than
     speculating.
  5. Nestasia (own_brand) vs. tracked competitors is a STRUCTURAL split in
     the evidence, not something inferred from a name in a flat list -- a
     real test run had the model include "Nestasia" inside a list of
     "confirmed clean competitors" it built itself, because the earlier
     evidence shape was one flat dict and nothing marked Nestasia as
     different in kind. Evidence is now always wrapped in
     {"own_brand": ..., "tracked_competitors": ...}; the prompt tells the
     model explicitly never to count own_brand entries when answering a
     "how many competitors" question.

Two rounds of real testing so far (see review_extraction/NOTES.md, "RAG
layer" section) found the following, and both prompt wording and evidence
field names were updated in response -- but this is reported honestly as a
measured improvement, not a claim that a small local model now never
hallucinates:
  - Round 1: `false_count` / `true_count` / `confirmed_clean` (ambiguous,
    collision-prone names) were misread on field-dense questions, and the
    model twice INVENTED an `all_collection_complete=false` value that the
    real evidence did not contain. Fields were renamed
    (skus_confirmed_clean, skus_with_confirmed_mismatch, brand_confirmed_
    clean, etc.) and the safety net below was rebuilt to actually check
    cited values against evidence, not just check that *a* citation exists.
  - Round 2 results are in NOTES.md, not asserted here in the docstring,
    since this file was written before that re-run happened.

Safety net for the smaller local model, rebuilt after round 1 found it was
giving false confidence (it passed 3 answers containing real hallucinated
field values because each one still happened to contain a citation-shaped
string somewhere). It now does real, if limited, value verification: it
scans the answer text for "<field_name> is/are/: <true|false|number>"
patterns, matches each one to the nearest brand name mentioned before it,
and checks that value against the actual evidence dict for that brand. A
mismatch is flagged by name: "UNVERIFIED -- cited value does not match
evidence: Milton.all_collection_complete: model said False, evidence says
True". This is NOT a full claim-parser -- see safety_check()'s docstring
for its real limitations (paraphrased claims that never use the literal
field name, e.g. "the collection isn't finished", are invisible to it).

Usage:
    python generate.py "Does Milton have the same stock-display bug we do?"

Requires Ollama installed and running locally, with a model already pulled
(see check_ollama() below -- this script checks and reports rather than
silently failing or trying to auto-install anything).
"""
import json
import re
import sys
import urllib.error
import urllib.request

from retrieve import retrieve, _json_default

OLLAMA_HOST = "http://localhost:11434"
MODEL = "llama3.1:8b"

SYSTEM_PROMPT = """You are a competitive-intelligence assistant for Nestasia, a home & kitchen brand. You answer questions using ONLY the structured evidence provided below, retrieved live from Nestasia's own tracking database. You do not have any other knowledge about these brands, their websites, or their behavior -- everything you know for this answer is in the EVIDENCE block.

Follow these rules exactly:

1. CITE EVERY FACTUAL CLAIM. For every number, price, count, or yes/no claim you make, name the source_type and collected_at date it came from (e.g. "per live_storefront data collected 2026-09-23"). If the evidence does not contain something needed to answer, say plainly: "We don't have this data" -- do not guess, estimate, or use outside knowledge about these brands.

2. RESPECT collection_complete / all_complete / any_partial. If any evidence group is marked collection_complete=false, all_complete=false, or any_partial=true, you MUST say the data for that group is PARTIAL/incomplete and that any count or conclusion from it could be an undercount. Never present partial data as if it were the full picture. Never state a value for all_collection_complete (or any other field) that you have not actually read from the evidence block -- if you are not sure, quote the evidence's own value verbatim rather than paraphrasing it from memory.

3. THE EVIDENCE IS STRUCTURALLY SPLIT INTO "own_brand" (Nestasia -- always exactly one entry, this is US) AND "tracked_competitors" (every other brand). NEVER include anything from "own_brand" when answering a question about "competitors" -- Nestasia is not a competitor of itself. When counting or listing competitors, only iterate over the entries inside "tracked_competitors".

4. RESPECT THE FOUR STOCK_MISMATCH STATES EXACTLY. Evidence for stock-mismatch questions includes a pre-computed classification per brand with these exact fields: brand_testable_for_mismatch (bool), brand_has_confirmed_mismatch (bool), brand_confirmed_clean (bool), skus_with_confirmed_mismatch (count), skus_confirmed_clean (count), skus_not_applicable_na (count), skus_ambiguous_unknown (count). Use these fields directly, do not recompute them yourself:
   - If brand_testable_for_mismatch=false: the correct answer is "this cannot currently be tested on this brand's platform" -- NOT "no bug" and NOT "yes bug". This happens when skus_not_applicable_na equals the total SKU count for that brand.
   - If brand_has_confirmed_mismatch=true: state plainly that a real mismatch was confirmed, and cite skus_with_confirmed_mismatch and example SKUs if given.
   - If brand_confirmed_clean=true: state that this brand was tested and found clean -- distinguish this clearly from "untestable" (brand_testable_for_mismatch=false is a completely different situation from brand_confirmed_clean=true, do not confuse them).
   - When asked to aggregate across tracked_competitors (e.g. "how many competitors have this bug"), you MUST report three separate groups by name, using only the tracked_competitors section: (a) brands with brand_has_confirmed_mismatch=true, (b) brands with brand_testable_for_mismatch=false ("cannot be tested" -- report this group separately, do NOT fold it into either of the other two groups), (c) brands with brand_confirmed_clean=true. Give the correct count for each group and make sure your count matches the number of names you actually list -- recount before answering.

5. REFUSE ANYTHING REQUIRING INTERNAL DATA. This database has no sales, revenue, margin, or units-sold data. If evidence.intent is "unsupported_internal_data" or the evidence is empty for this reason, respond with a clear refusal explaining this system only has publicly-collected storefront and ad data, and does not have access to internal business metrics. Do not attempt to estimate or infer an answer.

6. If the retrieval intent is "unknown" or evidence is empty for any other reason, say you don't have data to answer this question rather than answering from general knowledge.

Write a direct, concise answer (a few sentences, or a short list if enumerating SKUs/brands). Always end factual answers with the sources cited inline, not just a bare summary."""


def check_ollama():
    """Returns (ok, message). Checks Ollama is reachable AND that MODEL is
    pulled -- both are checked here rather than assumed, per instructions
    to stop and tell the user rather than fail deep inside a generation
    call."""
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, ConnectionRefusedError, OSError) as e:
        return False, (
            "Ollama does not appear to be installed or running on this machine "
            f"({e}). Install it from https://ollama.com, then run:\n"
            f"    ollama pull {MODEL}\n"
            "before running this script."
        )
    models = [m["name"] for m in data.get("models", [])]
    if not any(m == MODEL or m.startswith(MODEL.split(":")[0] + ":") for m in models):
        return False, (
            f"Ollama is running, but {MODEL!r} is not pulled. Available models: {models or '(none)'}.\n"
            f"Run:\n    ollama pull {MODEL}\nthen try again."
        )
    return True, f"Ollama OK, model available: {models}"


def _call_ollama(prompt, system=None):
    payload = json.dumps({
        "model": MODEL,
        "prompt": prompt,
        "system": system if system is not None else SYSTEM_PROMPT,
        "stream": False,
        "options": {"temperature": 0.1},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/generate", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    return data["response"]


# ---------------------------------------------------------------------------
# Safety net, round 2: actually verify cited field values against evidence.
#
# Round 1's version only checked that *some* citation-shaped string (a
# source_type keyword or an ISO date) appeared anywhere in the answer. That
# passed all 8 answers in the first real test run, including 3 that
# contained a genuinely invented field value (the model stated
# all_collection_complete=false for two brands whose real evidence says
# true) -- because each of those answers also happened to mention a real
# date or "live_storefront" elsewhere in the same text. A citation existing
# is not the same as a citation being correct; that gap is exactly what
# this rebuild targets.
#
# Approach and its real limits (reported honestly, not oversold):
#   - Regex-scans the answer for "<known_field_name> is/are/:/= <true|false|
#     number>" patterns. This only catches claims that use (something close
#     to) the literal field name -- which is what actually happened in every
#     hallucination seen so far (the model tends to echo evidence field
#     names verbatim, sometimes even as raw JSON fragments), but a fully
#     paraphrased claim ("the collection isn't finished yet") that never
#     uses the field name would NOT be caught. This is a deliberate,
#     documented middle ground, not a general-purpose claim extractor --
#     building one of those would mean parsing arbitrary free text into
#     logical propositions, a much bigger project than this pass.
#   - Attributes each matched claim to the nearest known brand name
#     mentioned earlier in the same answer (within a fixed character
#     window). This is a heuristic, not a real coreference resolver -- a
#     claim about brand A sandwiched between two mentions of brand B could
#     misattribute. In practice, answers are short and mention one brand at
#     a time, so this held up across the actual re-test (see NOTES.md).
#   - Ground truth is read directly from the SAME evidence dict retrieve.py
#     produced for this question -- not re-queried, not approximated.
# ---------------------------------------------------------------------------
_CITATION_MARKERS = (
    "live_storefront", "ad_library_public", "founder_confirmed", "industry_report",
    "marketplace_review", "qcommerce_listing", "search_signal",
)
_DATE_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_REFUSAL_MARKERS = ("don't have", "do not have", "no data", "can't answer", "cannot answer",
                     "internal data", "unable to answer", "cannot currently be tested",
                     "can't currently be tested", "cannot be tested")

_KNOWN_BRANDS = ("Nestasia", "Wonderchef", "Home Centre", "Milton", "Prestige", "Borosil")

_TRACKED_FIELD_NAMES = (
    "all_collection_complete", "brand_testable_for_mismatch", "brand_has_confirmed_mismatch",
    "brand_confirmed_clean", "skus_with_confirmed_mismatch", "skus_confirmed_clean",
    "skus_not_applicable_na", "skus_ambiguous_unknown", "all_complete", "any_partial",
    "collection_complete", "total_skus", "priced_skus", "sku_count",
)
_FIELD_VALUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in _TRACKED_FIELD_NAMES) + r")\b"
    # Tolerates "X is false", "X: false", "X field being false", "X field
    # value is false" -- the model uses all of these in practice (verified
    # against round 1's real transcripts, not guessed). Still won't catch a
    # fully paraphrased claim with no field name at all -- see the
    # docstring above this section.
    r"[\"']?\s*(?:field\s+)?(?:value\s+)?(?:is|are|:|=|being)\s*[\"']?\s*(true|false|\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _flatten_evidence_to_entities(evidence):
    """Walks the {"own_brand": ..., "tracked_competitors": ...} shape and
    returns {brand_name: {field_name: value}} across both sections, for
    every scalar (bool/int/float) field found."""
    entities = {}

    def add(name, d):
        if not name or not isinstance(d, dict):
            return
        bucket = entities.setdefault(name, {})
        for k, v in d.items():
            if isinstance(v, bool) or isinstance(v, (int, float)):
                bucket.setdefault(k, v)

    def walk_section(section):
        if isinstance(section, dict) and "classification" in section:
            for brand, cls in section["classification"].items():
                add(brand, cls)
            for row in section.get("example_mismatched_skus", []) or []:
                add(row.get("brand_name"), row)
        elif isinstance(section, list):
            for row in section:
                add(row.get("brand_name"), row)

    if isinstance(evidence, dict) and "own_brand" in evidence:
        walk_section(evidence["own_brand"])
        walk_section(evidence["tracked_competitors"])

    return entities


def _entities_for_claim(text, match_start, match_end, entity_names, window=400):
    """Returns the list of brand names a field=value claim at this position
    should be checked against.

    Two shapes, both seen in real model output:
      1. "<Brand>: <field> is <value>" or "<Brand> has <field>=<value>" --
         the brand precedes the claim in the same sentence. Handled by
         looking backward for the nearest entity name.
      2. "Brands with <field>=<value>: \\n - <Brand A> ...\\n - <Brand B> ..."
         -- a header stating the condition ONCE, followed by a bulleted list
         of every brand that meets it. A backward-only search here silently
         grabs whichever brand was named in the PREVIOUS, unrelated
         sentence -- confirmed as a real false positive in testing (round 2,
         Q6): the model's own correct answer used this list-header shape,
         and the backward-only heuristic mis-attributed two of its claims
         to the wrong brand, flagging a good answer as a hallucination.
         Detected by a colon shortly after the claim, and handled by
         collecting every known entity name mentioned in the block that
         follows (until the next blank-line-delimited section), checking
         the claim against ALL of them -- a shared header value must hold
         for each brand listed under it, not just the first.

    Still a heuristic, not a real parser -- see the module docstring for
    what this can't catch (e.g. a claim about brand A sandwiched textually
    between two unrelated mentions of brand B in the same sentence).
    """
    # Look for a colon within the rest of this line, not just the next few
    # characters -- "brand_testable_for_mismatch=false, we have:" puts 13+
    # characters between the value and the colon that actually marks this
    # as a list header. A too-narrow window here (an earlier version used
    # 6 chars) misses this shape entirely and falls through to the
    # backward-only search below, silently mis-attributing the claim --
    # confirmed as the real cause of a second false positive in testing
    # (round 3, Q6, "brand_testable_for_mismatch=false, we have:" wrongly
    # attributed to Borosil instead of Home Centre).
    line_end = text.find("\n", match_end)
    tail = text[match_end:line_end if line_end != -1 else min(len(text), match_end + 60)]
    if ":" in tail:
        # The header's own trailing blank line ("...we have:\n\n* Home
        # Centre...") sits BEFORE the actual list -- stopping at the first
        # "\n\n" (an earlier version of this function did) cuts the block
        # off before it reaches any entity name at all, and the code below
        # then silently fell back to the backward search, mis-attributing
        # the claim to whatever brand was named in the unrelated PRECEDING
        # sentence. Confirmed as a second real false positive in testing
        # (round 3, Q6). Fixed by bounding the forward block at the next
        # mention of any tracked field name (signalling a new header/claim
        # starts there) or a fixed window, instead of the first blank line.
        search_end = min(len(text), match_end + window)
        next_field_pos = None
        for f in _TRACKED_FIELD_NAMES:
            idx = text.find(f, match_end + 1, search_end)
            if idx != -1 and (next_field_pos is None or idx < next_field_pos):
                next_field_pos = idx
        hard_end = next_field_pos if next_field_pos is not None else search_end

        # For the LAST header in a sequence (no further field name ahead to
        # bound it), the block would otherwise run all the way to the
        # window limit -- far enough to swallow a trailing summary sentence
        # ("Therefore, ... is 3 (Borosil)") that mentions a brand from an
        # EARLIER, unrelated header. Confirmed as a third real false
        # positive in testing (round 3, Q6). Fixed by additionally cutting
        # the block off at the end of the bullet list itself: a blank line
        # that is NOT immediately followed by another bullet marks the list
        # as finished.
        segment = text[match_end:hard_end]
        bullet_re = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
        lines = segment.split("\n")
        kept, i = [], 0
        while i < len(lines):
            if lines[i].strip() == "":
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j < len(lines) and bullet_re.match(lines[j]):
                    i = j
                    continue
                break
            kept.append(lines[i])
            i += 1
        block = "\n".join(kept)
        found = [(block.find(name), name) for name in entity_names if name in block]
        found = [x for x in found if x[0] != -1]
        if found:
            found.sort()
            return [name for _, name in found]

    start = max(0, match_start - window)
    segment = text[start:match_start]
    best_name, best_idx = None, -1
    for name in entity_names:
        idx = segment.rfind(name)
        if idx != -1 and (start + idx) > best_idx:
            best_idx = start + idx
            best_name = name
    return [best_name] if best_name else []


def _parse_claimed_value(raw):
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        return float(raw) if "." in raw else int(raw)
    except ValueError:
        return raw


def _values_match(claimed, actual):
    if isinstance(actual, bool):
        return isinstance(claimed, bool) and claimed == actual
    if isinstance(actual, (int, float)) and isinstance(claimed, (int, float)) and not isinstance(claimed, bool):
        return abs(float(claimed) - float(actual)) < 0.01
    return str(claimed).lower() == str(actual).lower()


def safety_check(answer_text, evidence):
    """Returns (passed, reason). See module docstring for the approach and
    its real limits. Order of checks:
      1. Any field=value claim that can be matched to a brand AND that
         field exists in that brand's real evidence -- verify it. Any
         mismatch fails the whole answer, named explicitly.
      2. If no such claim was found (nothing to verify) but the answer is a
         refusal / "can't be tested" response, pass -- no fact is asserted.
      3. Otherwise fall back to the old citation-presence check, honestly
         labeled as weaker ("no specific field=value claims were
         verifiable") so it's clear this isn't the same as (1) passing.
    """
    entities = _flatten_evidence_to_entities(evidence)
    entity_names = list(entities.keys()) or list(_KNOWN_BRANDS)

    mismatches = []
    checked = 0
    for m in _FIELD_VALUE_RE.finditer(answer_text):
        field = m.group(1).lower()
        claimed = _parse_claimed_value(m.group(2))
        for entity in _entities_for_claim(answer_text, m.start(), m.end(), entity_names):
            if entity not in entities or field not in entities[entity]:
                continue
            checked += 1
            actual = entities[entity][field]
            if not _values_match(claimed, actual):
                mismatches.append(f"{entity}.{field}: model said {claimed!r}, evidence says {actual!r}")

    if mismatches:
        return False, "UNVERIFIED -- cited value does not match evidence: " + "; ".join(mismatches)

    lowered = answer_text.lower()
    if any(marker in lowered for marker in _REFUSAL_MARKERS):
        return True, "refusal/no-data response -- no citation required"

    if checked:
        return True, f"verified {checked} cited field value(s) against evidence, all matched"

    has_source_type = any(marker in lowered for marker in _CITATION_MARKERS)
    has_date = bool(_DATE_RE.search(answer_text))
    if has_source_type or has_date:
        return True, "citation marker found (no specific field=value claims were verifiable against evidence)"
    return False, "no source_type or dated citation found anywhere in the answer"


# ---------------------------------------------------------------------------
# Structural fix for aggregate/multi-entity questions (round 3+).
#
# Root cause of Q6's remaining unreliability, established across 3 rounds of
# real testing: retrieve.py's bucketing (brand_has_confirmed_mismatch /
# brand_testable_for_mismatch / brand_confirmed_clean) was correct in
# EVERY run -- the errors (Nestasia re-appearing as a "competitor", Milton
# silently dropped, miscounted lists) only ever happened when the LLM was
# asked to re-narrate six brands' worth of state across a free-text
# multi-paragraph answer. A better prompt or a stricter safety net treats
# the symptom; the actual fix is to stop asking the model to hold and
# restate that state at all.
#
# For "stock_mismatch_aggregate" (currently the only intent that asks the
# model to count/bucket across ALL tracked brands at once, rather than look
# up one or two) the bucket membership and brand names are filled into a
# fixed template directly by Python, from the exact same
# evidence["tracked_competitors"]["classification"] dict retrieve.py always
# produces correctly. own_brand is never even read by this code path --
# not filtered out by instruction, structurally absent from the loop, so
# Nestasia cannot appear in a competitor bucket no matter what.
#
# The model's only remaining role is one short introductory sentence, and
# even that is checked before being allowed through: if it contains any
# digit, any known brand name, or any of a fixed set of fact-shaped words
# (see _is_safe_framing_sentence), it's discarded and a fixed default
# sentence is used instead. This directly answers the brief's question
# "does the optional framing sentence ever contradict or undermine the
# template's correct facts" -- it can't, because anything that could is
# filtered out before assembly, not caught after the fact.
#
# Other intents that COULD in principle span multiple brands
# (price_comparison, sku_count_by_category, completeness_check with no
# brand filter) were deliberately NOT moved to this templated path: none of
# them scored an error in any test round so far, because none of them ask
# the model to sort brands into named categorical buckets -- they're flat
# per-row facts, not a counting/classification task. Templating them now
# would be fixing something that isn't broken, which the brief explicitly
# warned against for the single-brand-lookup questions; the same caution
# applies here.
# ---------------------------------------------------------------------------
_AGGREGATE_TEMPLATED_INTENTS = {"stock_mismatch_aggregate"}

# ---------------------------------------------------------------------------
# Framing sentence: NOT model-generated. An earlier version of this file had
# the model write a free-text intro sentence and validated it with
# _is_safe_framing_sentence() (a keyword-based filter: reject anything with
# a digit, a known brand name, or one of a fixed list of "fact-shaped"
# words like "confirmed"/"mismatch"/"bug"). Directly unit-testing that
# filter against synthetic adversarial sentences -- the same discipline
# used to verify the safety net's earlier fixes against real failure text,
# not just assumed to work -- found it let two real failure classes
# straight through:
#   - "Most of your competitors are performing well on this metric" --
#     a fact claim paraphrased with none of the listed words. The filter
#     is a fixed keyword list; a small local model can rephrase around any
#     fixed list indefinitely, and this is exactly the shape of rephrase
#     that would.
#   - "Great news -- no issues found anywhere!" -- an outright
#     CONTRADICTION of the template (which says 2 brands have a confirmed
#     mismatch), and still no digit, brand name, or listed word for the
#     filter to catch. This is the more serious failure: not just an
#     unverifiable claim, but a wrong one sitting directly above correct
#     facts.
# Only 3 of 5 synthetic test cases were actually caught (see
# review_extraction/NOTES.md's RAG section for the full test). A broader
# keyword/semantic list would still be a fixed list a model can route
# around; it narrows the gap without closing it. Given this framing
# sentence exists purely for cosmetic variety and contributes nothing a
# user needs, the fix is the more restrictive option the brief itself
# raised: stop generating it at all. It's chosen from a short, fully
# pre-approved, hand-read set of sentences that assert nothing -- there is
# no text here an LLM produced or could still slip a claim into.
# ---------------------------------------------------------------------------
_APPROVED_FRAMING_SENTENCES = (
    "Here's the current picture across your tracked competitors, computed directly from the database:",
    "The following is computed directly from your live tracking data:",
    "Here's what the database currently shows across your tracked competitors:",
)

_FACT_SHAPED_WORDS = (
    "confirmed", "mismatch", "testable", "clean", "bug", "tested",
    "cannot", "can't", "has the", "does have", "does not have",
)


def _choose_framing_sentence(question):
    """Deterministic, not random -- the same question always gets the same
    sentence, which keeps output reproducible for testing/auditing without
    needing any model call at all."""
    idx = sum(ord(c) for c in question) % len(_APPROVED_FRAMING_SENTENCES)
    return _APPROVED_FRAMING_SENTENCES[idx]


def _is_safe_framing_sentence(sentence):
    """Kept for its unit tests and as a defensive check on
    _APPROVED_FRAMING_SENTENCES itself (see test coverage) -- no longer
    used to validate model output, since none is generated for this path
    anymore. Still: a framing sentence is safe only if it asserts nothing
    checkable -- no digits, no brand name, none of a fixed list of
    fact-shaped words. Confirmed by direct testing (not assumed) that this
    keyword-based check alone is NOT sufficient to catch a paraphrased or
    contradicting claim -- see the section docstring above for why the
    live path no longer relies on it."""
    if not sentence or len(sentence) > 200:
        return False
    if any(ch.isdigit() for ch in sentence):
        return False
    lowered = sentence.lower()
    if any(b.lower() in lowered for b in _KNOWN_BRANDS):
        return False
    if any(w in lowered for w in _FACT_SHAPED_WORDS):
        return False
    return True


def _bucket_tracked_competitors(evidence):
    """Buckets ONLY evidence['tracked_competitors']['classification'] --
    own_brand is never read here, which is what structurally guarantees
    Nestasia cannot end up in a competitor bucket for this path (a Python
    loop that never iterates over it, not a prompt instruction the model
    could still ignore)."""
    competitors = (evidence or {}).get("tracked_competitors", {}).get("classification", {})
    has_bug, untestable, clean, unclassified = [], [], [], []
    for name, cls in sorted(competitors.items()):
        entry = {
            "name": name,
            "skus_with_confirmed_mismatch": cls.get("skus_with_confirmed_mismatch", 0),
            "skus_confirmed_clean": cls.get("skus_confirmed_clean", 0),
            "skus_not_applicable_na": cls.get("skus_not_applicable_na", 0),
            "source_types": cls.get("source_types") or [],
            "latest_collected_at": cls.get("latest_collected_at"),
            "all_collection_complete": cls.get("all_collection_complete"),
        }
        if cls.get("brand_has_confirmed_mismatch"):
            has_bug.append(entry)
        elif not cls.get("brand_testable_for_mismatch"):
            untestable.append(entry)
        elif cls.get("brand_confirmed_clean"):
            clean.append(entry)
        else:
            # Shouldn't happen given retrieve.py's own classification logic
            # (every brand falls into exactly one of the three cases above)
            # -- but if a future change ever produces a brand matching none
            # of them, surface it explicitly rather than silently dropping
            # it from the count.
            unclassified.append(entry)
    return has_bug, untestable, clean, unclassified


def _format_bucket_entry(entry, bucket):
    date = entry["latest_collected_at"]
    date_str = date.split("T")[0] if date else "unknown date"
    src = ", ".join(entry["source_types"]) or "unknown source"
    partial_note = "" if entry.get("all_collection_complete") else " [PARTIAL data -- collection incomplete]"
    if bucket == "bug":
        detail = f"{entry['skus_with_confirmed_mismatch']} confirmed mismatched SKU(s)"
    elif bucket == "untestable":
        detail = f"{entry['skus_not_applicable_na']} SKU(s) not applicable / no comparable signal on this platform"
    elif bucket == "clean":
        detail = f"{entry['skus_confirmed_clean']} SKU(s) confirmed clean"
    else:
        detail = "UNCLASSIFIED -- matches none of the three known buckets, needs manual review"
    return f"{entry['name']} ({detail}; {src} data collected {date_str}){partial_note}"


def render_stock_mismatch_aggregate_template(evidence):
    """Builds the entire factual body of the answer in Python -- brand
    names, counts, and bucket membership are never generated by the model
    for this intent. See the section docstring above for why."""
    has_bug, untestable, clean, unclassified = _bucket_tracked_competitors(evidence)
    total = len(has_bug) + len(untestable) + len(clean) + len(unclassified)

    def join(entries, bucket):
        return "; ".join(_format_bucket_entry(e, bucket) for e in entries) if entries else "none"

    lines = [
        f"Of {total} tracked competitors:",
        f"- {len(has_bug)} show a CONFIRMED stock-display mismatch: {join(has_bug, 'bug')}.",
        f"- {len(untestable)} CANNOT currently be tested on their platform (not counted as clean): "
        f"{join(untestable, 'untestable')}.",
        f"- {len(clean)} were tested and CONFIRMED CLEAN: {join(clean, 'clean')}.",
    ]
    if unclassified:
        lines.append(f"- {len(unclassified)} did not match any known bucket, flagged for manual review: "
                      f"{join(unclassified, 'unclassified')}.")
    return "\n".join(lines)


def _generate_templated(question, intent, evidence):
    template_body = render_stock_mismatch_aggregate_template(evidence)

    # No model call at all for the intro -- see the section docstring above
    # _APPROVED_FRAMING_SENTENCES for why free generation here was removed
    # rather than filtered.
    intro = _choose_framing_sentence(question)
    answer = f"{intro}\n\n{template_body}"

    # The facts are Python-authored and the intro is drawn from a fixed,
    # pre-approved set -- nothing in this answer was generated by the model,
    # so this check is pure defense-in-depth (e.g. catching a future bug in
    # render_stock_mismatch_aggregate_template itself), not a check on any
    # model output.
    passed, reason = safety_check(answer, evidence)
    if not passed:
        answer = f"[UNVERIFIED -- needs human review: {reason}]\n\n{answer}"

    return {
        "question": question,
        "intent": intent,
        "evidence": evidence,
        "answer": answer,
        "safety_check_passed": passed,
        "safety_check_reason": reason,
        "templated": True,
        "framing_sentence_model_generated": False,
    }


def generate(question):
    evidence_result = retrieve(question)
    intent = evidence_result["intent"]

    if intent in _AGGREGATE_TEMPLATED_INTENTS:
        return _generate_templated(question, intent, evidence_result["evidence"])

    evidence_json = json.dumps(evidence_result, indent=2, default=_json_default)

    prompt = (
        f"QUESTION: {question}\n\n"
        f"EVIDENCE (from retrieve.py, live production database query):\n{evidence_json}\n\n"
        "Answer the question using only this evidence, per the rules in your system prompt."
    )

    answer = _call_ollama(prompt)
    passed, reason = safety_check(answer, evidence_result["evidence"])

    if not passed:
        answer = f"[UNVERIFIED -- needs human review: {reason}]\n\n{answer}"

    return {
        "question": question,
        "intent": evidence_result["intent"],
        "evidence": evidence_result["evidence"],
        "answer": answer,
        "templated": False,
        "safety_check_passed": passed,
        "safety_check_reason": reason,
    }


if __name__ == "__main__":
    ok, msg = check_ollama()
    print(msg)
    if not ok:
        sys.exit(1)

    q = " ".join(sys.argv[1:]) or "Does Milton have the same stock-display bug we do?"
    result = generate(q)
    print()
    print("=" * 70)
    print("QUESTION:", result["question"])
    print("INTENT:", result["intent"])
    print("=" * 70)
    print(result["answer"])
    print()
    print(f"[safety check: {'PASSED' if result['safety_check_passed'] else 'FLAGGED'} -- {result['safety_check_reason']}]")
