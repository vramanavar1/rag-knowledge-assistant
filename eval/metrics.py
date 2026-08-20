"""Scoring for the evaluation harness.

Three families, matching what the assignment asks for:

**Retrieval** -- hit@1, hit@5, MRR, document recall and section hit rate.
These are computed against the *retrieved passages*, independently of what the
model then wrote, because a generation failure and a retrieval failure need
different fixes and averaging them together hides which one you have.

**Generation** -- answer correctness, groundedness, citation accuracy,
hallucination rate and abstention accuracy.  Correctness is deliberately
checked two ways: a deterministic key-fact match that cannot drift, and an
optional LLM judge for phrasing the string match would miss.  When the two
disagree the report shows both rather than picking a winner.

**System** -- latency, tokens and estimated cost per question.

The two rules that make these numbers honest:

* ``forbidden_facts`` -- a versioning question is only correct if the answer
  contains $65 AND does not contain $59.  Without the negative check, an answer
  that hedges by quoting both prices scores as correct.
* ``hallucination`` -- answering at all is a failure when the corpus has no
  answer, no matter how good the prose is.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# Statuses that all mean "the system correctly declined to answer".
NON_ANSWER_STATUSES = {"insufficient_evidence", "denied"}

# A system with no abstention *mechanism* can still decline in prose: the
# baseline profile always reports status "answered", but its text sometimes
# says "the provided sources do not include information about...". Judging that
# as a hallucination would overstate the baseline's failure rate by counting
# three correct refusals as inventions. Both profiles are therefore scored on
# what they actually said, not only on their status field.
_DECLINE_RE = re.compile(
    r"(sources?|context|documents?|knowledge base|information)\s+(?:provided\s+)?"
    r"(?:do|does|did)\s*n[o']t\s+"
    r"(?:include|mention|contain|specify|provide|list|cover|address|state)"
    r"|(?:do|does|did)\s*n[o']t\s+(?:include|mention|contain|specify|provide|list)"
    r"\s+(?:any\s+)?information"
    r"|not\s+(?:explicitly\s+)?(?:listed|mentioned|specified|included|covered|"
    r"provided|available|defined|stated|detailed)\s+(?:in|within)\s+"
    r"(?:the\s+)?(?:provided\s+)?(?:sources?|context|documents?)"
    r"|no\s+information\s+(?:is\s+)?(?:available|provided|found)"
    r"|(?:cannot|can\s*not|unable to)\s+(?:find|determine|answer|locate)"
    r"|do\s*n[o']t\s+have\s+enough"
    r"|insufficient_evidence"
    r"|^\s*no,?\s+the\s+company\s+does\s+not\s+(?:offer|provide|have)",
    re.I | re.M,
)


def expresses_decline(text: str) -> bool:
    """Does the answer text decline to answer, whatever its status field says?"""
    return bool(_DECLINE_RE.search(text or ""))


def normalise(text: str) -> str:
    text = text.lower().replace(",", "").replace("$", "")
    return re.sub(r"\s+", " ", text)


def match_fact(answer: str, fact: str) -> bool:
    """Does the answer state this fact?

    ``fact`` may list alternatives separated by ``|``.  Numeric facts are
    matched on digit boundaries so that "30" does not match "300" and "65" does
    not match "650" -- without that, nearly every numeric check passes by
    accident on a corpus this full of figures.
    """
    haystack = normalise(answer)
    for alternative in fact.split("|"):
        needle = normalise(alternative).strip()
        if not needle:
            continue
        if re.fullmatch(r"[\d.]+%?", needle):
            # Boundaries must reject "99" inside "99.5" while still accepting
            # "30" at the end of a sentence ("...is 30."). So: not adjacent to a
            # digit, and not adjacent to a decimal point that has digits on the
            # far side.
            pattern = (
                rf"(?<!\d)(?<!\d\.){re.escape(needle)}(?!\d)(?!\.\d)"
            )
            if re.search(pattern, haystack):
                return True
        elif needle in haystack:
            return True
    return False


@dataclass
class CaseResult:
    id: str
    category: str
    difficulty: str
    question: str
    department: str

    status: str = ""
    expected_status: str = ""
    answer: str = ""
    confidence: float = 0.0

    # retrieval
    retrieved_docs: list[str] = field(default_factory=list)
    retrieved_sections: list[str] = field(default_factory=list)
    expected_docs: list[str] = field(default_factory=list)
    hit_at_1: bool | None = None
    hit_at_5: bool | None = None
    section_hit: bool | None = None
    doc_recall: float | None = None
    mrr: float | None = None

    # generation
    key_facts_missing: list[str] = field(default_factory=list)
    forbidden_facts_present: list[str] = field(default_factory=list)
    forbidden_docs_cited: list[str] = field(default_factory=list)
    cited_docs: list[str] = field(default_factory=list)
    correct: bool = False
    status_correct: bool = False
    hallucinated: bool = False
    declined_in_prose: bool = False
    citation_correct: bool | None = None
    citation_precision: float | None = None
    groundedness: float | None = None
    judge_score: int | None = None          # 0 wrong, 1 partial, 2 correct
    judge_reason: str = ""

    # system
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    cost_usd: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _section_matches(expected: str, retrieved: Iterable[str]) -> bool:
    """Match on the section number when there is one, else on the title text."""
    expected_norm = normalise(expected)
    number = re.match(r"^\s*([\d.]+)", expected)
    for candidate in retrieved:
        candidate_norm = normalise(candidate)
        if expected_norm in candidate_norm or candidate_norm in expected_norm:
            return True
        if number:
            candidate_number = re.match(r"^\s*([\d.]+)", candidate)
            if candidate_number and candidate_number.group(1) == number.group(1):
                return True
    return False


def score_case(
    case: dict[str, Any],
    answer: Any,
    latency_ms: float,
    top_k: int = 5,
) -> CaseResult:
    expected_docs = case.get("expected_docs") or []
    expected_sections = case.get("expected_sections") or []
    expected_status = case.get("expected_status", "answered")

    result = CaseResult(
        id=case["id"],
        category=case.get("category", "uncategorised"),
        difficulty=case.get("difficulty", "medium"),
        question=case["question"],
        department=case.get("department", "all"),
        status=answer.status,
        expected_status=expected_status,
        answer=answer.text,
        confidence=answer.confidence,
        expected_docs=expected_docs,
        latency_ms=round(latency_ms, 1),
    )

    # ---- retrieval -----------------------------------------------------
    ranked_docs: list[str] = []
    for hit in answer.hits:
        if hit.chunk.doc_id not in ranked_docs:
            ranked_docs.append(hit.chunk.doc_id)
    result.retrieved_docs = ranked_docs
    result.retrieved_sections = [h.chunk.section_path for h in answer.hits[:top_k]]

    if expected_docs:
        top_docs = ranked_docs[:top_k]
        result.hit_at_1 = bool(ranked_docs[:1]) and ranked_docs[0] in expected_docs
        result.hit_at_5 = any(d in expected_docs for d in top_docs)
        result.doc_recall = (
            len(set(top_docs) & set(expected_docs)) / len(set(expected_docs))
        )
        result.mrr = 0.0
        for rank, doc in enumerate(ranked_docs, start=1):
            if doc in expected_docs:
                result.mrr = 1.0 / rank
                break
        if expected_sections:
            result.section_hit = all(
                _section_matches(section, result.retrieved_sections)
                for section in expected_sections
            )

    # ---- generation ----------------------------------------------------
    result.cited_docs = sorted({c.doc_id for c in answer.citations})
    forbidden_docs = case.get("forbidden_docs") or []
    result.forbidden_docs_cited = [d for d in result.cited_docs if d in forbidden_docs]

    result.key_facts_missing = [
        fact for fact in (case.get("key_facts") or [])
        if not match_fact(answer.text, fact)
    ]
    result.forbidden_facts_present = [
        fact for fact in (case.get("forbidden_facts") or [])
        if match_fact(answer.text, fact)
    ]

    result.declined_in_prose = expresses_decline(answer.text)
    declined = answer.status in NON_ANSWER_STATUSES or result.declined_in_prose

    if expected_status in NON_ANSWER_STATUSES:
        result.status_correct = declined
    elif expected_status == "needs_clarification":
        # Asking for clarification is the only right move here; declining is not.
        result.status_correct = answer.status == "needs_clarification"
    else:
        result.status_correct = answer.status == expected_status and not declined

    if expected_status == "answered":
        result.correct = (
            answer.status == "answered"
            and not result.declined_in_prose
            and not result.key_facts_missing
            and not result.forbidden_facts_present
            and not result.forbidden_docs_cited
        )
        if result.cited_docs and expected_docs:
            overlap = len(set(result.cited_docs) & set(expected_docs))
            result.citation_precision = overlap / len(result.cited_docs)
            result.citation_correct = overlap > 0
        elif expected_docs:
            result.citation_correct = False
            result.citation_precision = 0.0
    else:
        # For a question with no answer, being correct IS declining to answer.
        result.correct = result.status_correct and not result.forbidden_facts_present

    # Answering a question the corpus cannot support, or stating a figure that
    # the case explicitly forbids, is a hallucination. Declining in prose is
    # not -- see expresses_decline.
    result.hallucinated = bool(
        (expected_status in NON_ANSWER_STATUSES and not declined)
        or result.forbidden_facts_present
    )

    groundedness = (answer.trace or {}).get("groundedness") or {}
    if "score" in groundedness:
        result.groundedness = groundedness["score"]

    trace = answer.trace or {}
    tokens = trace.get("tokens") or {}
    result.prompt_tokens = tokens.get("prompt", 0)
    result.completion_tokens = tokens.get("completion", 0)
    result.llm_calls = trace.get("llm_calls", 0)
    result.cost_usd = round(
        result.prompt_tokens / 1000 * 0.0025
        + result.completion_tokens / 1000 * 0.01,
        6,
    )
    return result


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(statistics.fmean(clean), 4) if clean else None


def _rate(values: list[bool | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(sum(1 for v in clean if v) / len(clean), 4) if clean else None


def aggregate(results: list[CaseResult]) -> dict[str, Any]:
    answered_cases = [r for r in results if r.expected_status == "answered"]
    non_answer_cases = [r for r in results if r.expected_status != "answered"]
    retrieval_cases = [r for r in results if r.expected_docs]

    summary: dict[str, Any] = {
        "cases": len(results),
        "retrieval": {
            "measured_on": len(retrieval_cases),
            "hit_at_1": _rate([r.hit_at_1 for r in retrieval_cases]),
            "hit_at_5": _rate([r.hit_at_5 for r in retrieval_cases]),
            "section_hit": _rate([r.section_hit for r in retrieval_cases]),
            "doc_recall": _mean([r.doc_recall for r in retrieval_cases]),
            "mrr": _mean([r.mrr for r in retrieval_cases]),
        },
        "generation": {
            "answer_correctness": _rate([r.correct for r in results]),
            "answer_correctness_answerable": _rate([r.correct for r in answered_cases]),
            "citation_correctness": _rate([r.citation_correct for r in results]),
            "citation_precision": _mean([r.citation_precision for r in results]),
            "groundedness": _mean([r.groundedness for r in results]),
            "hallucination_rate": _rate([r.hallucinated for r in results]),
            "status_accuracy": _rate([r.status_correct for r in results]),
            "abstention_accuracy": _rate([r.status_correct for r in non_answer_cases]),
            "judge_score": _mean(
                [r.judge_score / 2 for r in results if r.judge_score is not None]
            ),
        },
        "system": {
            "latency_p50_ms": None,
            "latency_p95_ms": None,
            "mean_prompt_tokens": _mean([float(r.prompt_tokens) for r in results]),
            "mean_completion_tokens": _mean([float(r.completion_tokens) for r in results]),
            "mean_llm_calls": _mean([float(r.llm_calls) for r in results]),
            "total_cost_usd": round(sum(r.cost_usd for r in results), 4),
            "cost_per_question_usd": round(
                sum(r.cost_usd for r in results) / max(len(results), 1), 6
            ),
        },
        "by_category": {},
    }

    latencies = sorted(r.latency_ms for r in results)
    if latencies:
        summary["system"]["latency_p50_ms"] = round(
            statistics.median(latencies), 1
        )
        index = min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))
        summary["system"]["latency_p95_ms"] = round(latencies[index], 1)

    categories = sorted({r.category for r in results})
    for category in categories:
        subset = [r for r in results if r.category == category]
        subset_retrieval = [r for r in subset if r.expected_docs]
        summary["by_category"][category] = {
            "cases": len(subset),
            "answer_correctness": _rate([r.correct for r in subset]),
            "hit_at_5": _rate([r.hit_at_5 for r in subset_retrieval]),
            "hallucination_rate": _rate([r.hallucinated for r in subset]),
            "mean_latency_ms": _mean([r.latency_ms for r in subset]),
        }

    return summary
