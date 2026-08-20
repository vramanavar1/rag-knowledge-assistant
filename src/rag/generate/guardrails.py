"""Guardrails: sufficiency, ambiguity, groundedness and confidence.

These four checks are what separate "a RAG demo" from something that can be
put in front of employees, and they map directly onto assignment Scenarios 4
and 5 and onto Step 5 Q6.

Sufficiency (Scenario 4)
    Decided from reranker scores, not from whether retrieval returned rows.
    Retrieval ALWAYS returns rows -- top-k is a fixed number, so asking about a
    severance policy that does not exist still yields five confident-looking
    chunks about leave and benefits.  The question is whether any of them
    actually answer, and that is what the reranker's 0-10 score measures.

Ambiguity (Scenario 5)
    Retrieve first, then ask.  Grounding the clarifying question in what the
    corpus actually contains means the options offered are real ones; asking
    before retrieving means guessing at what the user might have meant.
    Ambiguity is only reported when conversation history has not already
    resolved it -- condensation runs first, so "what is the limit" after a
    question about expenses is not ambiguous.

Groundedness (Step 5, Q6)
    Two layers.  A deterministic numeric check that every figure in the answer
    appears in the cited sources runs always -- in this corpus almost every
    dangerous hallucination is a wrong number, and a wrong number is cheap to
    catch without a model.  An LLM claim-by-claim check runs when available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rag.config import Settings
from rag.models import Hit
from rag.observability.tracing import get_logger
from rag.providers.llm import ChatProvider
from rag.retrieve.pipeline import RetrievalOutcome
from rag.text import tokenize

log = get_logger(__name__)

# Head nouns that name a *category* of value rather than a specific one.
_GENERIC_HEADS = {
    "limit", "limits", "cap", "caps", "maximum", "max", "minimum", "min",
    "threshold", "thresholds", "allowance", "amount", "rate", "rates",
    "cost", "costs", "price", "pricing", "fee", "fees", "deadline",
    "requirement", "requirements", "policy", "rule", "rules", "approval",
    "discount", "notice", "period", "sla",
}
_QUESTION_WORDS = {
    "what", "whats", "what's", "how", "much", "many", "is", "are", "the",
    "a", "an", "our", "my", "we", "i", "do", "does", "there", "for", "of",
    "can", "get", "to", "on", "in", "and", "it", "its", "that", "this",
}

_NUMERIC_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:\.\d+)?\s?%|\b\d[\d,]*(?:\.\d+)?\b"
)
_CITATION_RE = re.compile(r"\[(\d+)\]")


# --------------------------------------------------------------------------
# Sufficiency
# --------------------------------------------------------------------------


@dataclass
class SufficiencyVerdict:
    sufficient: bool
    reason: str = ""
    top_score: float = 0.0
    margin: float = 0.0


def assess_sufficiency(outcome: RetrievalOutcome,
                       settings: Settings) -> SufficiencyVerdict:
    if not outcome.hits:
        return SufficiencyVerdict(False, "no documents matched the query")

    scores = [
        h.rerank_score if h.rerank_score is not None else h.score
        for h in outcome.hits
    ]
    top = scores[0]
    margin = top - (scores[1] if len(scores) > 1 else 0.0)

    if top < settings.min_relevance:
        return SufficiencyVerdict(
            False,
            f"best passage scored {top:.1f}/10, below the "
            f"{settings.min_relevance:.1f} relevance floor",
            top_score=top,
            margin=margin,
        )
    return SufficiencyVerdict(True, "", top_score=top, margin=margin)


# --------------------------------------------------------------------------
# Ambiguity
# --------------------------------------------------------------------------


@dataclass
class AmbiguityVerdict:
    ambiguous: bool = False
    reason: str = ""
    options: list[str] = field(default_factory=list)


def _content_terms(query: str) -> list[str]:
    return [t for t in tokenize(query) if t not in _QUESTION_WORDS]


def detect_ambiguity(query: str, hits: list[Hit],
                     *, was_condensed: bool = False) -> AmbiguityVerdict:
    """Flag questions that name a category of value without saying which one."""
    if not hits:
        return AmbiguityVerdict()

    terms = _content_terms(query)
    # A question that survived condensation with plenty of specifics is not vague.
    if len(terms) > 4:
        return AmbiguityVerdict()
    if not any(t in _GENERIC_HEADS for t in terms):
        return AmbiguityVerdict()
    # Any term that is not a generic head is a qualifier ("hotel", "expense"),
    # and one qualifier is usually enough to pin the question down.
    qualifiers = [t for t in terms if t not in _GENERIC_HEADS]
    if len(qualifiers) >= 2:
        return AmbiguityVerdict()

    top = hits[:5]
    departments = {h.chunk.department for h in top}
    documents = {h.chunk.doc_id for h in top}
    scores = [h.rerank_score if h.rerank_score is not None else h.score for h in top]
    spread = (scores[0] - scores[min(2, len(scores) - 1)]) if scores else 0.0

    scattered = len(departments) >= 2 or len(documents) >= 3

    if not qualifiers:
        # The question is nothing but a generic head -- "what is the limit?".
        # There is no topic to be confident about, so a decisive reranker score
        # is not evidence that the question was understood: it just means the
        # ranker picked one of several equally valid readings. Scatter alone
        # settles it.
        if not scattered:
            return AmbiguityVerdict()
    else:
        # One qualifier usually pins the question down, so only treat it as
        # ambiguous when the ranker also failed to separate the candidates.
        if not (scattered and spread < 1.5):
            return AmbiguityVerdict()

    options: list[str] = []
    seen: set[str] = set()
    for hit in top:
        label = f"{hit.chunk.section_path} — {hit.chunk.title} ({hit.chunk.department})"
        key = f"{hit.chunk.doc_id}|{hit.chunk.section_path}"
        if key not in seen:
            seen.add(key)
            options.append(label)

    log.info(
        "ambiguous query detected",
        query=query,
        departments=sorted(departments),
        spread=round(spread, 2),
        was_condensed=was_condensed,
    )
    return AmbiguityVerdict(
        ambiguous=True,
        reason=(
            f"'{query}' matched {len(documents)} documents across "
            f"{len(departments)} departments with no clear winner"
        ),
        options=options[:5],
    )


# --------------------------------------------------------------------------
# Groundedness
# --------------------------------------------------------------------------


@dataclass
class GroundednessVerdict:
    score: float = 1.0                       # 0.0 - 1.0
    method: str = "numeric"
    unsupported_figures: list[str] = field(default_factory=list)
    unsupported_claims: list[str] = field(default_factory=list)
    citations_valid: bool = True
    invalid_markers: list[int] = field(default_factory=list)


def _normalise_number(token: str) -> str:
    return token.replace("$", "").replace(",", "").replace(" ", "").rstrip(".").lower()


def check_numeric_grounding(answer: str, sources: list[Hit]) -> list[str]:
    """Every figure in the answer must appear in the cited source text.

    Cheap, deterministic, and it catches the failure mode that matters most
    here: a fluent answer quoting $59 when the current rate card says $65.
    """
    corpus = " ".join(hit.chunk.text for hit in sources)
    known = {_normalise_number(m.group(0)) for m in _NUMERIC_RE.finditer(corpus)}

    unsupported: list[str] = []
    for match in _NUMERIC_RE.finditer(answer):
        raw = match.group(0)
        value = _normalise_number(raw)
        if value in known:
            continue
        # Citation markers and small ordinals are not factual figures.
        if answer[max(0, match.start() - 1):match.start()] == "[":
            continue
        if value.isdigit() and int(value) <= 12 and "%" not in raw and "$" not in raw:
            continue
        unsupported.append(raw)
    return unsupported


def validate_citations(answer: str, source_count: int) -> tuple[bool, list[int]]:
    markers = [int(m.group(1)) for m in _CITATION_RE.finditer(answer)]
    invalid = sorted({m for m in markers if m < 1 or m > source_count})
    return (not invalid and bool(markers)), invalid


def verify_groundedness(
    answer: str,
    sources: list[Hit],
    context: str,
    llm: ChatProvider,
) -> GroundednessVerdict:
    verdict = GroundednessVerdict()

    verdict.unsupported_figures = check_numeric_grounding(answer, sources)
    verdict.citations_valid, verdict.invalid_markers = validate_citations(
        answer, len(sources)
    )

    score = 1.0
    if verdict.unsupported_figures:
        score -= min(0.6, 0.3 * len(verdict.unsupported_figures))
    if not verdict.citations_valid:
        score -= 0.2

    if llm.available:
        result = llm.complete(
            [
                {"role": "system", "content":
                 "You check whether an answer is supported by its sources.\n"
                 'Return JSON only: {"claims": [{"claim": "<short>", '
                 '"supported": true|false}], "verdict": "grounded"|'
                 '"partially_grounded"|"unsupported"}\n'
                 "Be strict: a claim is supported only if a source states it."},
                {"role": "user",
                 "content": f"Sources:\n\n{context}\n\nAnswer:\n{answer}"},
            ],
            temperature=0.0,
            max_tokens=500,
            json_mode=True,
            utility=True,
        )
        parsed = result.json() if result else None
        if isinstance(parsed, dict) and isinstance(parsed.get("claims"), list):
            claims = parsed["claims"]
            unsupported = [
                str(c.get("claim", ""))[:120]
                for c in claims
                if isinstance(c, dict) and not c.get("supported", True)
            ]
            verdict.unsupported_claims = unsupported
            verdict.method = "llm+numeric"
            if claims:
                supported_fraction = 1.0 - len(unsupported) / len(claims)
                score = min(score, supported_fraction)

    verdict.score = max(0.0, min(1.0, score))
    if verdict.unsupported_figures or verdict.unsupported_claims:
        log.warning(
            "answer not fully grounded",
            figures=verdict.unsupported_figures,
            claims=verdict.unsupported_claims[:3],
            method=verdict.method,
        )
    return verdict


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------


def compute_confidence(
    sufficiency: SufficiencyVerdict,
    groundedness: GroundednessVerdict,
) -> float:
    """Blend retrieval strength, decisiveness and groundedness into 0.0-1.0.

    Reported to the user and used to downgrade an answer to
    "insufficient evidence" when the pipeline succeeded mechanically but the
    evidence does not hold up.
    """
    retrieval = min(1.0, sufficiency.top_score / 10.0)
    margin = max(0.0, min(1.0, sufficiency.margin / 4.0))
    citations = 1.0 if groundedness.citations_valid else 0.0

    return round(
        0.45 * retrieval
        + 0.15 * margin
        + 0.30 * groundedness.score
        + 0.10 * citations,
        3,
    )
