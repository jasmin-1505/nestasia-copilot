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

## Chunk 2 -- wiring into the UI (NOT DONE)

`rag/app.py` has **not** been touched by this work yet. It still renders
the old raw `key=value` data dumps and the two `st.metric` "Accuracy"/
"Scope" badges from the previous round, not the new presenter bundles.

Remaining for a future session:
- [ ] Wire `presenters.PRESENTERS[question_id]` into `app.py`'s card
      renderer (`build_demo_card` / `build_live_card` in `app.py`), so the
      headline/visual/suggestion/firm_up/caveat/sources bundle becomes the
      primary card content instead of the raw data dump. The raw dump can
      move into a "Details" expander rather than being deleted outright
      (useful for debugging).
- [ ] Render `visual` bundles as actual Streamlit elements: `st.dataframe`
      for `{"type":"table",...}` (with per-column real/demo tagging shown
      somehow -- e.g. a coloured header or a caption), `st.bar_chart` (or
      a custom chart) for `{"type":"bar",...}`.
- [ ] Render `sources` as a list of citations (label + as_of + kind),
      distinct from the current single citation string.
- [ ] Render `caveat` prominently (not buried) wherever it's non-None --
      this is exactly the kind of thing a screenshot needs to carry with
      it, per the project's established "a screenshot of one card must be
      self-explanatory" requirement.
- [ ] Re-run `test_app.py` (the AppTest-based UI test suite) once app.py
      is updated, to confirm all 33 questions still render every required
      element with the new presenter-driven cards.
- [ ] Minor known rough edge to look at while wiring this in: `demo_20`'s
      headline can read `"...on the theme 'None'"` when an ad has no
      theme recorded -- not a wrong number (the test correctly passes it,
      since `None` isn't a numeric claim), but worth a `theme or
      "no stated theme"` fallback for readability once this is in the UI.

## Files touched by this work

- `rag/presenters.py` (new)
- `rag/test_presenters.py` (new)
- `PROGRESS.md` (this file, new)

`rag/app.py` intentionally left untouched, per instruction.
