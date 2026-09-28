"""
Routes a raw, typed question to a registered question_registry.py entry --
or explicitly refuses to. Uses local sentence-transformers embeddings
(all-MiniLM-L6-v2, free, no API, runs on CPU) to match against ONLY the
questions registered for the caller's active mode. Never answers a
question from the wrong mode's data, and never answers a below-threshold
"nearest" question just because it's the closest thing on file.

Three possible outcomes, always one of exactly these shapes:

  matched(question_id, confidence, top_alternatives)
      Route with confidence -- caller can now call
      question_registry.call_handler(question_registry.get(question_id), typed_text).

  wrong_mode(question_id, which_mode_answers_it)
      The best match overall belongs to the OTHER mode and clears the
      threshold there. The caller must not answer it from the active
      mode's data -- surface which mode actually answers it instead. This
      is the mechanism that stops a real-data question from being
      answered with synthetic fixture numbers, and the reverse.

  unsupported(nearest_questions)
      Nothing in the active mode clears the threshold. nearest_questions
      is informational only (what's closest, for a UI to suggest) -- it is
      NEVER auto-answered.

Live mode's 8 questions are still parameterized by brand/category the way
retrieve.py/generate.py already do it: the router only decides WHICH
registered question_id a typed string maps to (and whether that's even
allowed in the active mode); entity extraction inside that question stays
entirely retrieve.py's classify_intent() job, invoked later via
question_registry.call_handler(entry, typed_text) with the user's actual
wording, not rebuilt here.
"""
import os

import numpy as np

import question_registry as qr

_MODEL_NAME = "all-MiniLM-L6-v2"
_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".router_embedding_cache.npz")

# Tuned in the Step-4 test harness (test_router.py) to get zero false
# accepts and zero cross-mode leaks on the paraphrase/unsupported/
# cross-mode test set, even at the cost of some false rejects -- a false
# accept (wrong question answered, or an unsupported one answered as if
# supported) is worse than a false reject (a real question incorrectly
# called unsupported).
#
# 0.671 is the tightest threshold that still clears every false-accept
# confidence found in testing: the worst offender was "Do any of our own
# listings have a stock mismatch bug?" matching the WRONG live question
# (live_6, the cross-brand aggregate) at 0.6703 instead of the right one
# (live_2, Nestasia-only) at 0.6128 -- these near-duplicate questions,
# which differ only by which brand they name, are the hardest case for a
# pure sentence-embedding matcher (see test_router.py's report). Going
# below ~0.67 lets that kind of same-topic/wrong-brand confusion back in
# as a false accept; going above it (e.g. 0.68) rejects more good matches
# without buying any additional accept-safety. 0.671 is the minimum that
# is strictly greater than every observed false-accept score.
MATCH_THRESHOLD = 0.671

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _embed(texts):
    model = _get_model()
    return model.encode(list(texts), normalize_embeddings=True)


def _cosine(a, b):
    return float(np.dot(a, b))


class _EmbeddingCache:
    """Caches REGISTRY's own question-text embeddings on disk, keyed by
    question id + text (so an edited question text invalidates its own
    cached vector, nothing else's). Rebuilt automatically, once, whenever
    the registry doesn't match what's cached -- no manual invalidation
    step for whoever edits question_registry.py."""

    def __init__(self):
        self._ids = None
        self._texts = None
        self._vectors = None

    def _current_registry_signature(self):
        return [(e["id"], e["text"]) for e in qr.REGISTRY]

    def _load_from_disk(self):
        if not os.path.exists(_CACHE_FILE):
            return None
        try:
            data = np.load(_CACHE_FILE, allow_pickle=True)
            ids = list(data["ids"])
            texts = list(data["texts"])
            vectors = data["vectors"]
            return ids, texts, vectors
        except Exception:
            return None

    def _save_to_disk(self, ids, texts, vectors):
        np.savez(_CACHE_FILE, ids=np.array(ids, dtype=object), texts=np.array(texts, dtype=object), vectors=vectors)

    def ensure_fresh(self):
        wanted = self._current_registry_signature()
        wanted_ids = [w[0] for w in wanted]
        wanted_texts = [w[1] for w in wanted]

        cached = self._load_from_disk()
        if cached is not None:
            ids, texts, vectors = cached
            if ids == wanted_ids and texts == wanted_texts:
                self._ids, self._texts, self._vectors = ids, texts, vectors
                return

        vectors = _embed(wanted_texts)
        self._save_to_disk(wanted_ids, wanted_texts, vectors)
        self._ids, self._texts, self._vectors = wanted_ids, wanted_texts, vectors

    def vector_for_id(self, question_id):
        idx = self._ids.index(question_id)
        return self._vectors[idx]

    def all_ids(self):
        return list(self._ids)

    def all_vectors(self):
        return self._vectors


_cache = _EmbeddingCache()


def _rank_all(typed_text):
    """Returns [(question_id, similarity)] sorted descending, across the
    WHOLE registry (both modes) -- mode filtering happens after ranking,
    not before, specifically so wrong_mode can be detected."""
    _cache.ensure_fresh()
    query_vec = _embed([typed_text])[0]
    ids = _cache.all_ids()
    vectors = _cache.all_vectors()
    sims = vectors @ query_vec
    ranked = sorted(zip(ids, sims.tolist()), key=lambda t: t[1], reverse=True)
    return ranked


def route(typed_text, active_mode):
    """The single entry point. active_mode must be 'demo' or 'live' --
    same never-infer-never-default discipline as retrieve.py's db_mode."""
    if active_mode not in ("demo", "live"):
        raise ValueError(f"active_mode must be 'demo' or 'live', got {active_mode!r}")

    ranked = _rank_all(typed_text)
    ranked_by_id = dict(ranked)

    same_mode_ranked = [(qid, sim) for qid, sim in ranked if qr.get(qid)["mode"] == active_mode]
    other_mode_ranked = [(qid, sim) for qid, sim in ranked if qr.get(qid)["mode"] != active_mode]

    best_same = same_mode_ranked[0] if same_mode_ranked else (None, -1.0)
    best_other = other_mode_ranked[0] if other_mode_ranked else (None, -1.0)

    # wrong_mode takes priority over a same-mode match below threshold, and
    # ALSO over a same-mode match if the other mode's best match is
    # actually the stronger one -- never silently prefer the active mode
    # just because it's active. This is what stops cross-mode leaks.
    if best_other[1] >= MATCH_THRESHOLD and best_other[1] > best_same[1]:
        other_mode = qr.get(best_other[0])["mode"]
        return {
            "outcome": "wrong_mode",
            "question_id": best_other[0],
            "which_mode_answers_it": other_mode,
            "confidence": round(best_other[1], 4),
        }

    if best_same[1] >= MATCH_THRESHOLD:
        alternatives = [{"question_id": qid, "confidence": round(sim, 4)}
                        for qid, sim in same_mode_ranked[1:4]]
        return {
            "outcome": "matched",
            "question_id": best_same[0],
            "confidence": round(best_same[1], 4),
            "top_alternatives": alternatives,
        }

    nearest = [{"question_id": qid, "confidence": round(sim, 4), "mode": qr.get(qid)["mode"]}
               for qid, sim in ranked[:5]]
    return {
        "outcome": "unsupported",
        "nearest_questions": nearest,
    }
