"""Version- and recency-aware ranking (assignment Scenario 3).

The failure this prevents
-------------------------
``Pricing2025.pdf`` and ``Pricing2026.pdf`` are near-identical documents.  Ask
"what does the Professional tier cost" and pure similarity has no reason to
prefer one over the other -- if anything the 2025 card can win, because it is
shorter and its wording is closer to the plain question (the 2026 card spends a
section on what changed).  The user gets $59 instead of $65, with a real
citation to a real document, which is the most dangerous failure mode in the
whole system.

The approach here
-----------------
Currency is decided at ingest time by :func:`rag.ingest.metadata.reconcile_versions`
and stored as a metadata field.  At query time:

* Superseded chunks are demoted, not deleted -- "what did we charge in 2025"
  and "what changed for 2026" are legitimate questions.
* When the question names a past year, the demotion inverts: chunks effective
  in that year are promoted instead.
* For an ordinary question, once a current document has answered, superseded
  chunks covering the same ground are dropped from the context entirely, so
  the model cannot blend two rate cards into one answer.

Note that this is ranking, not filtering-by-date: an "effective date in the
past" is normal for a live policy.  What matters is whether something newer
has explicitly replaced it.
"""

from __future__ import annotations

import re

from rag.models import Hit
from rag.observability.tracing import get_logger

log = get_logger(__name__)

SUPERSEDED_PENALTY = 3.0
HISTORICAL_MATCH_BONUS = 3.0
HISTORICAL_MISMATCH_PENALTY = 1.0

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_HISTORICAL_WORDS = re.compile(
    r"\b(previous|prior|old|older|former|last year|used to|historical|"
    r"before|superseded|legacy|back then|at the time)\b",
    re.I,
)
# Words that signal a question about change *over time*.
#
# "compare", "versus" and "difference between" are deliberately absent. They are
# entity-comparison words, not history words: "compare the Starter and
# Enterprise tiers" is a question about two tiers, not about two years, and
# treating it as historical readmits the superseded rate card and produces an
# answer quoting both 2025 and 2026 prices. A genuine version comparison names
# a year, which `explicit_year` already catches.
_CHANGE_WORDS = re.compile(
    r"\b(chang(?:e|ed|es|ing)|what'?s new|new for|increase[ds]?\s+(?:from|to)|"
    r"used to be|no longer|previously)\b",
    re.I,
)


def detect_temporal_intent(query: str) -> tuple[int | None, bool]:
    """Return ``(explicit_year, wants_history)``.

    ``explicit_year`` is a year named in the question.  ``wants_history`` is
    True when the question is about change over time or explicitly about the
    past, in which case superseded documents are legitimate evidence.
    """
    years = [int(m.group(0)) for m in _YEAR_RE.finditer(query)]
    explicit_year = years[0] if years else None
    wants_history = bool(
        _HISTORICAL_WORDS.search(query) or _CHANGE_WORDS.search(query)
    )
    return explicit_year, wants_history


def _effective_year(hit: Hit) -> int | None:
    date = hit.chunk.effective_date
    if not date:
        return None
    try:
        return int(date[:4])
    except ValueError:
        return None


def prefilter_superseded(hits: list[Hit], query: str) -> tuple[list[Hit], dict]:
    """Drop superseded chunks **before** reranking.

    This runs before the reranker rather than after it, and the ordering is not
    cosmetic. ``Pricing2025.pdf`` and ``Pricing2026.pdf`` contain near-identical
    tier tables. When both reach the reranker, it has no basis to prefer one and
    will sometimes score the *2025* table 10/10 and the 2026 table 0/10 --
    observed exactly that. Filtering afterwards then discards the only candidate
    the reranker was confident about, leaving nothing above the relevance floor,
    and the system abstains on a question it can answer.

    Filtering first means the reranker never sees the duplicate, spends its
    candidate slots on distinct content, and its scores survive intact.

    Superseded chunks are kept when the question is historical (names a past
    year, or asks what changed), and when no current document covers the same
    ground -- a superseded document is still the best available answer if
    nothing replaced the section being asked about.
    """
    explicit_year, wants_history = detect_temporal_intent(query)
    if explicit_year is not None or wants_history:
        return hits, {
            "explicit_year": explicit_year,
            "wants_history": wants_history,
            "kept_superseded": True,
            "dropped_superseded": 0,
        }

    current_cover = {
        (h.chunk.department, h.chunk.doc_type) for h in hits if h.chunk.is_current
    }
    kept, dropped = [], []
    for hit in hits:
        if (not hit.chunk.is_current
                and (hit.chunk.department, hit.chunk.doc_type) in current_cover):
            dropped.append(hit.chunk.chunk_id)
            continue
        kept.append(hit)

    if dropped:
        log.info("superseded chunks dropped before reranking",
                 count=len(dropped), query=query)

    return kept, {
        "explicit_year": explicit_year,
        "wants_history": wants_history,
        "kept_superseded": False,
        "dropped_superseded": len(dropped),
    }


def apply_version_ranking(hits: list[Hit], query: str) -> tuple[list[Hit], dict]:
    """Score adjustment for document currency, applied after reranking.

    By this point :func:`prefilter_superseded` has already removed superseded
    duplicates for non-historical questions, so this stage only has to handle
    the two remaining cases: promoting the right year when the question names
    one, and demoting anything superseded that survived the prefilter.
    """
    explicit_year, wants_history = detect_temporal_intent(query)

    for hit in hits:
        boost = 0.0
        year = _effective_year(hit)

        if explicit_year is not None and year is not None:
            boost += (HISTORICAL_MATCH_BONUS if year == explicit_year
                      else -HISTORICAL_MISMATCH_PENALTY)
        elif not hit.chunk.is_current and not wants_history:
            boost -= SUPERSEDED_PENALTY

        hit.recency_boost = boost
        base = hit.rerank_score if hit.rerank_score is not None else hit.score
        hit.score = base + boost

    hits.sort(key=lambda h: -h.score)
    return hits, {
        "explicit_year": explicit_year,
        "wants_history": wants_history,
        "boosted": sum(1 for h in hits if h.recency_boost > 0),
        "demoted": sum(1 for h in hits if h.recency_boost < 0),
    }
