# Nestasia Competitive Intelligence -- Web UI

## Running it

Prerequisites:
- Python deps: `pip install streamlit sentence-transformers` (plus this
  project's existing `psycopg2`, `numpy`).
- `.env` at the repo root with the `PRODUCTION_DB_*` / `FIXTURE_DB_*`
  fields (same as every other script in `db/` and `rag/`).
- For Live mode's 6 non-templated questions: [Ollama](https://ollama.com)
  running locally with `llama3.1:8b` pulled (`ollama pull llama3.1:8b`).
  Demo mode uses no LLM at all and works with Ollama stopped.

Launch:

```bash
cd rag
streamlit run app.py
```

Opens at `http://localhost:8501`.

## Switching modes

The **Mode** control is in the sidebar (Demo / Live). Switching modes does
not clear the conversation -- every past answer keeps the mode label (and
its own accuracy/scope badges, citation, and disclaimers) it was produced
under, so scrolling back never shows a demo answer looking like a live one
or vice versa. A question typed in the wrong mode is never guessed at: the
router (`router.py`) tells you which mode actually answers it instead of
answering from the wrong data source.

## Adding a question

Adding question #26 (demo) or a 9th live question never touches
`router.py`, `app.py`, or any routing logic:

1. **Demo question**: write a handler function in
   `fixture_business_queries.py` returning the standard shape (`supported`,
   `data`, `sources`, `gap_explanation`, `special_note`) that every one of
   the 25 existing handlers returns, add it to `QUESTION_HANDLERS` in that
   file, then add one entry to `question_registry.py`'s demo section
   (`text`, `category`, `question_number`, `scope`, `notes`).
2. **Live question**: if it fits an existing `retrieve.py` intent
   (`classify_intent()`), no new handler code is needed at all -- just add
   one entry to `question_registry.py`'s `_LIVE_QUESTIONS` list
   (`arg_style="typed_text"`, routed through `generate()`, which re-parses
   brand/category entities from whatever the user actually typed). A
   genuinely new intent needs a new handler in `retrieve.py` first, same as
   any of the original 8.
3. Delete `rag/.router_embedding_cache.npz` (or just let it auto-rebuild --
   `router.py` detects the registry changed and re-embeds automatically).

No UI code, no router matching logic, and no mode-separation logic need to
change for either case.

## Testing

```bash
python test_router.py   # router alone, no UI -- paraphrase/unsupported/cross-mode test set
python test_app.py      # drives app.py end-to-end via streamlit.testing.v1.AppTest, no browser
```

`test_app.py`'s first question in a fresh session pays a one-time ~60-90s
model-load cost (sentence-transformers loading inside the test harness);
every question after that in the same run is ~1-2s.
