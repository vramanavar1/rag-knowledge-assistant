"""Reranking (assignment Scenario 1, and Step 5 Q1).

Retrieval and ranking are different jobs.  Hybrid search is a *recall* device:
its job is to make sure the right chunk is somewhere in the top 20, and it is
cheap enough to run over the whole index.  It is not good at deciding which of
those 20 actually answers the question, because BM25 and cosine similarity both
score "is this about the same topic", not "does this contain the answer".

That gap is exactly the shape of the "5 chunks retrieved, 1 relevant" problem.
The fix is a second, more expensive pass over a small candidate set that scores
relevance directly.  Two implementations:

``llm_rerank``       -- a cheap model scores each candidate 0-10 against the
                        query.  This is the local stand-in for the Azure AI
                        Search semantic ranker; when the Azure backend is
                        active its L2 reranker does the same job in-service.
``lexical_rerank``   -- deterministic fallback that scores query-term coverage,
                        weighting section-heading matches, so the system still
                        reranks with no model available.

The scores also feed the sufficiency gate: a top score below ``min_relevance``
means the corpus probably does not contain the answer (Scenario 4).
"""

from __future__ import annotations

from rag.models import Hit
from rag.observability.tracing import get_logger
from rag.providers.llm import ChatProvider
from rag.text import analyze, stem

log = get_logger(__name__)

MAX_CANDIDATES = 20
SNIPPET_CHARS = 700

_RERANK_SYSTEM = """You score how well each passage answers a specific question.

Return JSON only: {"scores": [{"id": <int>, "score": <0-10>}, ...]} with one entry per passage.

Scoring guide:
10 = contains the complete answer explicitly
7-9 = contains most of the answer, or the exact figure/rule asked for
4-6 = same topic and useful context, but does not state the answer
1-3 = same document or domain, but does not address the question
0 = irrelevant

Judge only whether the passage answers THIS question. Do not reward passages for being well written or for being about the same department."""

# Stemmed, because the analyzer stems both the query and the index; an
# unstemmed stopword list would let "does" through as "doe".
_STOPWORDS = {
    stem(word)
    for word in (
        "the", "a", "an", "is", "are", "was", "were", "what", "which", "who",
        "how", "do", "does", "did", "for", "of", "to", "in", "on", "at", "and",
        "or", "my", "i", "we", "our", "can", "if", "it", "be", "there", "any",
        "much", "many", "have", "has", "get", "am",
    )
}


def _content_terms(query: str) -> list[str]:
    return [t for t in analyze(query) if t not in _STOPWORDS and len(t) > 1]


def lexical_rerank(query: str, hits: list[Hit]) -> list[Hit]:
    """Deterministic relevance scoring on a 0-10 scale.

    Coverage of the query's content terms, with matches in the section heading
    weighted higher than matches in the body: a heading match is strong
    evidence that the chunk is *about* the question rather than merely
    mentioning its words in passing.
    """
    terms = _content_terms(query)
    if not terms:
        for hit in hits:
            hit.rerank_score = 5.0
        return hits

    for hit in hits:
        chunk = hit.chunk
        body_tokens = set(analyze(chunk.text))
        heading_tokens = set(analyze(f"{chunk.title} {chunk.section_path}"))

        body_hits = sum(1 for t in terms if t in body_tokens)
        heading_hits = sum(1 for t in terms if t in heading_tokens)

        coverage = body_hits / len(terms)
        heading_coverage = heading_hits / len(terms)

        score = 10.0 * (0.7 * coverage + 0.3 * heading_coverage)
        # Numeric questions are usually answered by the chunk holding the digits.
        if any(any(ch.isdigit() for ch in t) for t in terms):
            if any(t in body_tokens for t in terms if any(ch.isdigit() for ch in t)):
                score += 1.0
        hit.rerank_score = round(min(score, 10.0), 2)

    return hits


def llm_rerank(query: str, hits: list[Hit], llm: ChatProvider) -> list[Hit] | None:
    """Score candidates with a model. Returns ``None`` if the call fails."""
    if not hits:
        return hits

    passages = []
    for index, hit in enumerate(hits):
        chunk = hit.chunk
        snippet = chunk.text[:SNIPPET_CHARS]
        passages.append(
            f"[{index}] {chunk.title} > {chunk.section_path}\n{snippet}"
        )

    result = llm.complete(
        [
            {"role": "system", "content": _RERANK_SYSTEM},
            {
                "role": "user",
                "content": f"Question: {query}\n\nPassages:\n\n"
                           + "\n\n".join(passages),
            },
        ],
        temperature=0.0,
        max_tokens=600,
        json_mode=True,
        utility=True,
    )
    parsed = result.json() if result else None
    if not isinstance(parsed, dict):
        return None

    scores: dict[int, float] = {}
    for item in parsed.get("scores", []):
        try:
            scores[int(item["id"])] = float(item["score"])
        except (KeyError, TypeError, ValueError):
            continue
    if not scores:
        return None

    for index, hit in enumerate(hits):
        hit.rerank_score = max(0.0, min(10.0, scores.get(index, 0.0)))
    return hits


def _is_degenerate(hits: list[Hit]) -> bool:
    """True when the reranker gave every candidate a zero."""
    return bool(hits) and all((h.rerank_score or 0.0) <= 0.0 for h in hits)


def rerank(query: str, hits: list[Hit], llm: ChatProvider) -> tuple[list[Hit], str]:
    """Rerank candidates, returning ``(hits, method_used)``."""
    candidates = hits[:MAX_CANDIDATES]

    # Azure AI Search's semantic configuration already reranked these
    # server-side, and its L2 model is purpose-built for the job. Running our
    # own LLM reranker on top would add ~2s and a model call to re-derive an
    # answer we already have.
    if candidates and all(h.rerank_score is not None for h in candidates):
        return hits, "azure-semantic"

    method = "lexical"
    if llm.available:
        reranked = llm_rerank(query, candidates, llm)
        if reranked is None:
            lexical_rerank(query, candidates)
            log.warning("LLM rerank failed, used lexical fallback")
        elif _is_degenerate(reranked):
            # Every candidate scored zero. Occasionally the model returns this
            # for a question the corpus *can* answer -- a single rerank call is
            # a single point of failure, and when it collapses the sufficiency
            # gate abstains on an answerable question. Treat a total collapse
            # like a failed call and fall back to the deterministic scorer,
            # which still scores genuinely-absent topics below the floor.
            lexical_rerank(query, candidates)
            method = "lexical-after-degenerate-llm"
            log.warning(
                "LLM rerank returned no signal for any candidate; "
                "fell back to lexical scoring",
                query=query,
                candidates=len(candidates),
            )
        else:
            method = "llm"
    else:
        lexical_rerank(query, candidates)

    # Anything beyond the reranked window keeps a score below the window's
    # floor, so it can never outrank a scored candidate.
    for hit in hits[MAX_CANDIDATES:]:
        hit.rerank_score = 0.0

    return hits, method
