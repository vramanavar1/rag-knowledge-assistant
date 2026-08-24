"""Answer generation: grounded synthesis, abstention, clarification.

The generator's job is not only to write an answer but to decide whether an
answer should be written at all.  Four outcomes are possible and all four are
first-class:

``answered``               grounded, cited, with a confidence score
``insufficient_evidence``  the corpus does not support an answer (Scenario 4)
``needs_clarification``    the question names a category, not a value (Scenario 5)
``denied``                 nothing the caller is allowed to read matched (Step 5 Q4)

An answer that is generated and then fails verification is downgraded to
``insufficient_evidence`` rather than shipped with a caveat: a wrong figure
with a real citation is worse than no answer, because it is indistinguishable
from a right one at the point of use.
"""

from __future__ import annotations

import re

from rag.config import Settings
from rag.generate.guardrails import (
    assess_sufficiency,
    compute_confidence,
    detect_ambiguity,
    verify_groundedness,
)
from rag.generate.prompts import (
    BASELINE_ANSWER_SYSTEM,
    INSUFFICIENT,
    build_answer_messages,
    build_clarify_messages,
    build_context,
)
from rag.models import Answer, Citation, Hit, Principal, Turn
from rag.observability.tracing import get_logger, stage
from rag.providers.llm import ChatProvider
from rag.retrieve.pipeline import RetrievalOutcome
from rag.text import analyze as tokenize

log = get_logger(__name__)

MIN_CONFIDENCE = 0.35
_CITATION_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class AnswerGenerator:
    def __init__(self, settings: Settings, llm: ChatProvider) -> None:
        self._settings = settings
        self._llm = llm

    # ------------------------------------------------------------------

    async def generate(
        self,
        question: str,
        outcome: RetrievalOutcome,
        history: list[Turn] | None = None,
        principal: Principal | None = None,
    ) -> Answer:
        base_trace = dict(outcome.trace)
        base_trace["rerank_method"] = outcome.rerank_method

        if not outcome.hits:
            return self._nothing_found(outcome, principal, base_trace)

        if not self._settings.is_baseline:
            ambiguity = detect_ambiguity(
                outcome.standalone_query,
                outcome.hits,
                was_condensed=outcome.condensed,
            )
            if ambiguity.ambiguous:
                return await self._clarify(question, outcome, ambiguity, base_trace)

            sufficiency = assess_sufficiency(outcome, self._settings)
            base_trace["sufficiency"] = {
                "sufficient": sufficiency.sufficient,
                "top_score": round(sufficiency.top_score, 2),
                "margin": round(sufficiency.margin, 2),
                "reason": sufficiency.reason,
            }
            if not sufficiency.sufficient:
                return self._abstain(outcome, sufficiency.reason, base_trace)
        else:
            sufficiency = assess_sufficiency(outcome, self._settings)

        # Context assembly is its own stage: which chunks made the window, and
        # how much of the budget they consumed, is the first thing you want to
        # know when an answer is wrong but retrieval looked fine.
        with stage("context") as st:
            context, used = build_context(outcome.hits, self._settings.max_context_chars)
            st["sources"] = len(used)
            st["chars"] = len(context)
            st["budget_chars"] = self._settings.max_context_chars
            st["dropped"] = len(outcome.hits) - len(used)

        with stage("generate") as st:
            text, generator = await self._write(question, context, history)
            st["generator"] = generator
            st["sources"] = len(used)
        base_trace["generator"] = generator
        base_trace["context_chars"] = len(context)

        if text.strip().upper().startswith(INSUFFICIENT):
            return self._abstain(
                outcome, "the model reported the sources do not answer the question",
                base_trace
            )

        with stage("verify") as st:
            groundedness = await verify_groundedness(text, used, context, self._llm)
            st["groundedness"] = groundedness.score
            st["method"] = groundedness.method
        base_trace["groundedness"] = {
            "score": round(groundedness.score, 3),
            "method": groundedness.method,
            "unsupported_figures": groundedness.unsupported_figures,
            "unsupported_claims": groundedness.unsupported_claims[:5],
            "citations_valid": groundedness.citations_valid,
            "invalid_markers": groundedness.invalid_markers,
        }

        confidence = compute_confidence(sufficiency, groundedness)
        citations = self._extract_citations(text, used)

        # An answer that fails verification is withdrawn, not caveated.
        if not self._settings.is_baseline and confidence < MIN_CONFIDENCE:
            log.warning("answer withdrawn after verification",
                        confidence=confidence, question=question)
            return self._abstain(
                outcome,
                f"the drafted answer could not be verified against the sources "
                f"(confidence {confidence:.2f})",
                base_trace | {"withdrawn_answer": text[:400]},
            )

        return Answer(
            text=text.strip(),
            status="answered",
            citations=citations,
            confidence=confidence,
            hits=outcome.hits,
            standalone_query=outcome.standalone_query,
            subqueries=outcome.subqueries,
            trace=base_trace,
        )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    async def _write(
        self, question: str, context: str, history: list[Turn] | None
    ) -> tuple[str, str]:
        if self._llm.available:
            messages = build_answer_messages(question, context, history)
            if self._settings.is_baseline:
                messages[0] = {"role": "system", "content": BASELINE_ANSWER_SYSTEM}
            result = await self._llm.complete(
                messages, temperature=0.0, max_tokens=700
            )
            if result and result.text.strip():
                return result.text.strip(), "llm"
            log.warning("answer generation failed, using extractive fallback")

        return self._extractive(question, context), "extractive"

    @staticmethod
    def _extractive(question: str, context: str) -> str:
        """Deterministic fallback when no chat model is reachable.

        Returns the sentences from source [1] that best cover the question's
        terms, cited.  It is not a good answer -- it is an honest one, and the
        response trace records that no model wrote it.
        """
        first_block = context.split("\n\n")[0] if context else ""
        body = first_block.split("\n", 1)[1] if "\n" in first_block else first_block
        terms = set(tokenize(question))

        sentences = [s.strip() for s in _SENTENCE_RE.split(body) if s.strip()]
        scored = sorted(
            sentences,
            key=lambda s: -len(terms & set(tokenize(s))),
        )
        selected = [s for s in scored[:3] if terms & set(tokenize(s))] or sentences[:2]

        if not selected:
            return INSUFFICIENT
        return " ".join(selected) + " [1]"

    # ------------------------------------------------------------------
    # Non-answers
    # ------------------------------------------------------------------

    def _nothing_found(
        self, outcome: RetrievalOutcome, principal: Principal | None, trace: dict
    ) -> Answer:
        restricted = principal is not None and "*" not in principal.departments
        if restricted:
            text = (
                "I could not find anything on that in the documents available to "
                f"you ({', '.join(principal.departments)}). If this belongs to "
                "another department, ask its owner or request access."
            )
            status = "denied"
        else:
            text = ("I could not find anything about that in the knowledge base.")
            status = "insufficient_evidence"

        return Answer(
            text=text,
            status=status,
            confidence=0.0,
            standalone_query=outcome.standalone_query,
            subqueries=outcome.subqueries,
            trace=trace | {"reason": "no candidates after filtering"},
        )

    def _abstain(self, outcome: RetrievalOutcome, reason: str, trace: dict) -> Answer:
        nearest = ", ".join(
            dict.fromkeys(f"{h.chunk.title}" for h in outcome.hits[:3])
        )
        text = (
            "I don't have enough in the knowledge base to answer that reliably, "
            "so I'd rather not guess."
        )
        if nearest:
            text += f" The closest documents I found were: {nearest}."

        log.info("abstained", reason=reason, query=outcome.standalone_query)
        return Answer(
            text=text,
            status="insufficient_evidence",
            confidence=0.0,
            hits=outcome.hits,
            standalone_query=outcome.standalone_query,
            subqueries=outcome.subqueries,
            trace=trace | {"abstain_reason": reason},
        )

    async def _clarify(
        self, question: str, outcome: RetrievalOutcome, ambiguity, trace: dict
    ) -> Answer:
        text = ""
        if self._llm.available:
            result = await self._llm.complete(
                build_clarify_messages(question, ambiguity.options),
                temperature=0.0,
                max_tokens=250,
                utility=True,
            )
            if result:
                text = result.text.strip()

        if not text:
            listed = "\n".join(f"- {option}" for option in ambiguity.options)
            text = (
                "That could refer to a few different things in our documents. "
                f"Which did you mean?\n{listed}"
            )

        return Answer(
            text=text,
            status="needs_clarification",
            confidence=0.0,
            hits=outcome.hits,
            standalone_query=outcome.standalone_query,
            subqueries=outcome.subqueries,
            clarification_options=ambiguity.options,
            trace=trace | {"ambiguity_reason": ambiguity.reason},
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _extract_citations(text: str, used: list[Hit]) -> list[Citation]:
        citations: list[Citation] = []
        for marker in dict.fromkeys(
            int(m.group(1)) for m in _CITATION_RE.finditer(text)
        ):
            if not 1 <= marker <= len(used):
                continue
            chunk = used[marker - 1].chunk
            citations.append(
                Citation(
                    marker=f"[{marker}]",
                    doc_id=chunk.doc_id,
                    chunk_id=chunk.chunk_id,
                    title=chunk.title,
                    section_path=chunk.section_path,
                    page=chunk.page,
                    source_path=chunk.source_path,
                    quote=chunk.text[:280],
                )
            )
        return citations
