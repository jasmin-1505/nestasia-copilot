# Readable-answers work -- progress

Tracks the "make the UI's raw data dumps readable" effort layered on top of
the existing `rag/app.py` (Streamlit) + `rag/router.py` +
`rag/question_registry.py` stack. See `rag/README.md` for how to run the
app itself; this file tracks only the presenter/readability layer.

## Chunk 1 -- presenters + headline-number test (DONE)

- [x] `rag/presenters.py`: formatting helpers (`format_inr` with Indian
      lakh/crore grouping, `format_date`, `format_pct`) -- no LLM anywhere
      in the file.
- [x] All 25 demo presenters (`demo_1`..`demo_25`) and all 8 live
      presenters (`live_1`..`live_8`), each returning `headline`, `visual`,
      `suggestion`, `firm_up`, `sources`, `caveat`.
- [x] Live presenters call `retrieve.retrieve()` directly, bypassing
      `generate()`'s Ollama path entirely, so headlines are always
      computed f-strings, never model prose. (This also fixed two real
      defects found in the pre-existing UI: internal variable names
      `own_brand`/`tracked_competitors` and a markdown/code-span artifact
      were leaking into the LLM's live-mode answer text.)
- [x] Decision-shaped questions (Q8, Q13, Q17, Q21, Q22, Q25) say
      "candidates, ranked by `<metric>`", never "you should".
- [x] Caveats added for circularity (Q10, Q18), fabricated ad->product
      links (Q5's launch dates are also flagged as fabricated; Q16, Q17,
      Q21), and unknowable competitor margin (Q6). `live_4` and `live_6`
      also carry a caveat (untestable-brand handling).
- [x] `visual` is concrete: every presenter returns either
      `{"type":"table","columns":[...],"rows":[...]}` (columns are
      `{"label":..., "kind":"real"|"demo"}` dicts, tagged per-column
      wherever a table blends real and demo data -- e.g. Q6, Q16, Q18) or
      `{"type":"bar","labels":[...],"values":[...],"unit":"₹|%|units"}`.
- [x] `rag/test_presenters.py`:
  - Headline-number check over all 33 presenters, fixed to (a) strip
    every entity string (product/brand/category/channel names) out of the
    headline before extracting numbers, so "3000ml" inside a product name
    is never treated as a numeric claim, and (b) compare formatted string
    to formatted string (via the SAME `format_inr`/`format_pct`
    formatters the presenter used) instead of raw float-to-float with a
    tolerance. **Currently: PASSED, 33/33.**
  - `prove_test_catches_mutation()`: corrupts one number in a real
    headline and confirms the check now fails. **PASSED** (unmutated
    headline passes, mutated one is caught).
  - `test_no_directive_language()`: hard-fails on "you should"/"you
    must"/"you need to"/"we recommend" anywhere in headline/suggestion/
    firm_up/caveat; separately lists (does not fail on) any sentence
    starting with a bare imperative verb (Cut/Stop/Increase/Reduce/
    Prioritise) for human review. **Currently: 0 hard failures, 0
    sentences flagged for review.**
  - One real bug class found and fixed along the way: Postgres `NUMERIC`
    columns come back from psycopg2 as `decimal.Decimal`, not `float` --
    an earlier version of the number-extraction helper only recognized
    `int`/`float` and silently dropped every revenue/margin/percentage
    value from the "known-good" set, which is why the very first run
    reported nearly every currency/percent figure as "unverified" even
    though the presenters themselves were correct.

## Chunk 1.5 -- post-Chunk-1 fixes (DONE)

Several rounds of auditing and fixing landed between Chunk 1 and the
Chunk 2 rebuild below, all committed:
- [x] `q15_competitor_availability`'s `ILIKE '%disabled%'`/`'%sold%'`
      substring-matching bug fixed for all brands (false 100% for
      Borosil, false 42.7% for Milton -- both were matching negated
      phrases like "not disabled" and "sold-out badge visible on this
      card" describing an UNKNOWN state).
- [x] `live_2`/`live_6` completeness-caveat gaps found and fixed (Nestasia's
      and Wonderchef's own incomplete collections weren't mentioned even
      though the underlying evidence already carried
      `all_collection_complete=False`).
- [x] Audited all 25 demo presenters for the same pattern: 22 of 25
      silently aggregate over Nestasia's Cookware SKUs (confirmed
      under-collected, 12 of 88 real products) without a caveat --
      fixed via one shared fact (`incomplete_categories_included`,
      computed once per run) wired into each affected presenter.
      `q9`'s caveat further sharpened for its Cookware-vs-Bakeware
      pairwise comparison specifically.
- [x] Added a direction check to `test_presenters.py` (higher/lower/
      above/below/more/less/rising/declining/up/down/ahead/behind
      headlines verified against the underlying data) and a full,
      corrected mutation-proof + ranking-check pass across all 33.
      Found and fixed a real ranking bug in `present_demo_13` (named
      "Blinkit" as top quick-commerce channel when Instamart actually
      had higher revenue -- the handler filters but never sorts).
- [x] `Nestasia Cookware` marked `collection_complete=False` in both
      production and fixture (confirmed via a live site check: "88
      Cookware Results found" vs. 12 SKUs on file). Bakeware's
      collision between two site collections (`/collections/bakeware`
      vs. `/collections/serveware-bakeware`) resolved by explicit
      decision, logged in `review_extraction/NOTES.md` finding (g);
      its `collection_complete` flag deliberately left untouched
      pending further investigation (the 392-vs-3 gap doesn't fit the
      same pagination story as Cookware).

## Chunk 2 -- dropdown-based UI rebuild (DONE)

`rag/app.py` was rebuilt from scratch with a DROPDOWN as the primary way
to ask a question (not free text). Full spec delivered:

- [x] Sidebar: mode toggle (Live/Demo), a dropdown of valid questions for
      the active mode (demo: grouped `[Category] question text`; live:
      ungrouped, 8 questions), a secondary "Or ask something else
      (experimental)" text box routed through `router.py`, and a static
      "What this is" panel.
- [x] Persistent, non-dismissable mode banner at the top of every screen.
- [x] Each answer renders as one card in the specified order: chip strip
      (Mode / Data: Real|Demo|Mixed / Coverage: Complete|Partial|Not
      supported) -> headline -> visual (`st.dataframe`/`st.bar_chart` from
      the presenter's own bundle) -> suggestion (muted) -> "Read before
      relying on this" box -> "What would confirm this" line -> Sources
      (hyperlink if a real URL exists, else the label or "demo data") ->
      collapsed Details expander (matched question id + confidence, table
      columns, raw evidence) -> a one-line mode/data footer.
- [x] Every card has a disclaimer: the presenter's own caveat, or one of
      two fixed fallback lines when there isn't one -- "This answer uses
      fully verified, complete data." only when the card is both fully
      real AND fully complete, "No further caveat applies." otherwise
      (a judgment call: the instruction's two example fallbacks didn't
      cover a fully-synthetic-but-uncaveated case like Q19's brand-level
      social data, so I picked the honest option rather than the
      literal-but-misleading "fully verified" one).
  - **Reconciled an apparent contradiction between the spec's items 4 and
    5**: item 4 says the caveat box appears "only when a caveat exists";
    item 5 says every card needs a disclaimer line "rather than showing
    nothing." Implemented as: the box always renders, using the real
    caveat when there is one and a fixed fallback when there isn't --
    item 5's hard requirement (never show nothing) takes precedence.
- [x] `live_6` (templated aggregate) and `live_8` (refusal) render
      without calling Ollama at all -- confirmed this was NOT already
      true for `live_8` (generate.py's dispatch would have routed it
      through Ollama like every other non-templated intent) and added an
      explicit bypass building Details straight from `retrieve()`.
      The other 6 live questions still use Ollama as before; a plain
      "Live AI-generated answers need Ollama running locally -- start it
      and refresh." message replaces a crash when it's down. Demo mode
      never touches Ollama.
- [x] `.streamlit/config.toml`: light theme, cream background (#FBF6EC),
      terracotta primary (#B5745C), charcoal text (#2B2B28).
- [x] `rag/test_app.py` rewritten for the new card shape. **Current
      result: 0 failures across all checks** --
      33/33 dropdown questions render mode label + both chips + a
      disclaimer; 0 internal-name leaks (`collection_complete`,
      `is_synthetic`, `source_type`, `own_brand`, `tracked_competitors`)
      found outside the Details expander across all 33; all 3 known-good
      paraphrases matched and answered; both nonsense inputs showed the
      unsupported list AND were confirmed present in the gitignored JSONL
      log.
- [x] 4 screenshots taken and sent for review (empty first screen, a demo
      dropdown answer, a live dropdown answer, the free-text nonsense ->
      unsupported result) -- **not yet committed**, per instruction, until
      those are reviewed.

## Chunk 2.5 -- plain-language leak sweep + 5 UI fixes (DONE)

Two things landed together: fixing the `firm_up`-line leaks flagged above
(now resolved, not left open), and 5 UI fixes.

- [x] **`present_demo_6`/`7`/`9`'s shared `firm_up` leak fixed**: *"Confirm
      unit_cost against real supplier/COGS records -- margin_percent here
      is a synthetic estimate."* -> *"Confirm per-unit cost against real
      supplier/COGS records -- this margin figure is a synthetic
      estimate."*
- [x] **Full snake_case sweep, not just the 5 named terms**: scanned every
      presenter's `headline`/`suggestion`/`firm_up`/`caveat` against live
      data (all 33 questions) for ANY `word_word`-shaped token, not only
      the 5 originally-named internal fields. Found and fixed 6 more real
      leaks beyond the 3 the user named: `demo_1`, `demo_2` (dormant
      no-data branch), `demo_3`/`demo_24` (shared string), `demo_4`,
      `demo_8`, `demo_25` -- all rewritten in plain language (e.g.
      "Confirm current stock and units sold..." instead of "Confirm
      current_stock and units_sold..."). Re-scan: **0 matches**.
      `test_presenters.py` re-run clean after every change.
  - `test_app.py`'s leak check was itself upgraded from "check only the 5
    named terms" to also run a general `\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b`
    regex sweep over every card's public-facing text, so this whole class
    of leak can't silently regress again without a test failure.
- [x] **A second, previously-unaudited leak surface found while wiring up
      the new Coverage-missing line** (see next item): the `gap_explanation`
      strings in `fixture_business_queries.py` (feeding demo's `scope_missing`)
      and the `notes` strings in `question_registry.py` (feeding live's
      Coverage-missing line) had never been scanned, because nothing
      rendered them outside Details until now. Found 15 matches (demo_6,
      10, 16, 17, 18, 19, 21, plus all 8 live entries) and rewrote every
      one in plain language. Re-scan across presenters.py's 4 fields +
      scope_missing + notes, all 33 questions: **0 matches**.
  - **A third leak surface found the same way, one level deeper**: the
    Sources line was rendering raw fixture/production table names
    directly -- `sales_data (fixture)`, `margin_data (fixture)`,
    `price_history (real)`, `paid_ad_creative (real)`, `sku (real)`, etc.
    -- discovered only once `test_app.py`'s leak check was made general
    (see above) instead of scoped to the 5 named terms; the general sweep
    flagged 42 matches, all in `sources[].label`. Fixed with a
    `TABLE_LABELS` lookup in `presenters.py` (`sales_data` -> "Sales
    records", `margin_data` -> "Margin data", `price_history` ->
    "Competitor pricing", `paid_ad_creative` -> "Ad library", `sku` ->
    "Product catalogue", etc., falling back to a de-underscored,
    capitalized version of the table name for anything not in the map)
    used everywhere a source label is built. Re-ran the full `test_app.py`
    suite: **0 leaks, TOTAL FAILURES: 0**. (Note: demo-kind source labels
    were already being replaced with a generic "demo data" string at
    render time in `app.py`, so this specific leak was never actually
    visible on screen for demo sources -- but it was still present in the
    underlying card data that `test_app.py` deliberately checks at the
    data level, and it WAS visible on screen for the `kind="real"`
    literal labels like `price_history (real)`, so the fix was real and
    necessary either way.)
  - **Lesson applied going forward**: any new UI feature that surfaces a
    previously-internal-only field needs its own leak re-scan -- this
    happened twice in a row (Coverage-missing line -> scope_missing/notes;
    generalizing the leak-check itself -> source labels).
- [x] **5 UI fixes** (inferred from a shorthand list in chat; the full
      spec message that presumably preceded it wasn't visible when these
      were implemented -- flagging this as an assumption, not a confirmed
      match to unseen wording):
  1. **Coverage "Missing: `<reason>`" line**: `build_demo_card`/
     `build_live_card` now include `coverage_missing` (demo: from
     `generate.py`'s `_scope_missing_line()`; live: from the registry's
     `notes`, or "Nothing missing."/"Missing: not specified." fallbacks),
     rendered directly under the chip row. Fixed a double-prefix bug
     found via a manual screenshot (`_scope_missing_line()` already
     returns a complete "Missing: ..." sentence; `app.py` was adding a
     second "Missing: " on top of it).
  2. **Scrollable fixed-height tables**: `st.dataframe(..., height=...)`
     with `height = min(320, 38 + 35 * max(n_rows, 1))` -- short tables
     (e.g. the 8-row Q6 comparison) aren't padded with empty space, long
     ones (e.g. Q6's ~76-row live table) scroll inside a capped box
     instead of pushing the whole page down.
  3. **Clearing the answer area on mode switch**: `main()` now compares
     the sidebar radio's new value against `st.session_state.mode` and
     resets `current_card = None` on an actual change, so switching modes
     shows "Pick a question to get started" instead of the previous
     mode's stale card under the new banner.
  4. **Unsupported-list-renders-in-full**: new
     `test_unsupported_list_renders_in_full()` in `test_app.py` asserts
     the unsupported card's `grouped_questions` lists exactly 25 (demo) /
     8 (live) questions by counting at the data level, independent of
     `render_card`'s own loop -- confirmed passing, and visually confirmed
     in the live-mode screenshot (all 8 questions listed).
  5. **Title sizing**: replaced `st.title()` (oversized default h1) with
     a custom `<h2>`-scale `st.markdown(...)` heading.
- [x] `test_app.py` re-run in full after all fixes: **TOTAL FAILURES: 0**
      across all 33 dropdown questions (0 leaks under the new general
      snake_case sweep), 3/3 paraphrases, 2/2 nonsense+logged, and the new
      unsupported-list-full check (25/25 demo, 8/8 live).
- [x] 5 fresh screenshots taken and sent (empty state with the corrected
      title size; a demo answer showing chips + table + the new
      Coverage/Missing line together; a live answer; free-text nonsense ->
      unsupported showing the full 8-question list; the same demo card
      scrolled to Sources, showing the plain-language source labels after
      the table-name fix) -- **not yet committed**, per instruction, until
      these are reviewed.

**Known rough edges, reported honestly, not hidden or fixed without
asking:**
- Manual browser testing hit real Streamlit session-state flakiness
  (mode reverting, stale widget state surviving a server restart) that
  turned out to be an artifact of rapid manual clicking during
  screenshot-taking, not an app bug -- confirmed by the fully
  deterministic, 0-failure `AppTest` suite. Worth knowing about if a
  human clicks through the real UI quickly.
- The "5 UI fixes" were implemented from a shorthand list in the latest
  message; the fuller spec message it referred to wasn't visible in
  context at the time. If any of the 5 don't match what was actually
  asked for, that's why -- flag it and it can be adjusted.

## How to run it

```bash
cd rag
streamlit run app.py
```
Needs `pip install streamlit sentence-transformers pandas` (plus this
project's existing `psycopg2`, `numpy`) and, for Live mode's 6
LLM-backed questions, Ollama running locally with `llama3.1:8b` pulled.
Demo mode works with Ollama off.

## Files touched by this work

- `rag/presenters.py`, `rag/test_presenters.py` (Chunk 1 + 1.5)
- `rag/app.py` (rebuilt for Chunk 2)
- `rag/test_app.py` (rewritten for Chunk 2)
- `.streamlit/config.toml` (new, Chunk 2 theme)
- `review_extraction/NOTES.md` (Bakeware/Cookware findings, Chunk 1.5)
- `PROGRESS.md` (this file)
