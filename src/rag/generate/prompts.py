"""Prompts and context assembly.

Three things in here do real work beyond "here is some context, answer the
question":

1. **An explicit refusal token.**  The model is told to emit
   ``INSUFFICIENT_EVIDENCE`` verbatim rather than to "say you don't know".
   A sentinel is machine-checkable; a politely-worded non-answer is not, and
   the difference matters because the abstention rate is a measured metric.

2. **Currency labelling in the context.**  Each source is tagged CURRENT or
   SUPERSEDED with its effective date.  Superseded sources only reach the
   prompt for historical or comparative questions, and when they do the model
   must not blend their figures with current ones.

3. **Citations tied to source numbers, not document names.**  The model cites
   ``[2]``, which resolves to a specific chunk, so a citation can be verified
   against the exact text that was in the window.  "According to the Travel
   Policy" cannot be verified and is what a valid-looking-but-wrong citation
   looks like (assignment Step 5, Q6).
"""

from __future__ import annotations

from rag.models import Chunk, Hit, Turn

INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

ANSWER_SYSTEM = f"""You are the Northwind Traders internal knowledge assistant. You answer employee questions using ONLY the numbered sources provided.

Rules:
1. Use only the numbered sources. Never use outside knowledge about companies, laws, or common practice.
2. Every sentence that states a fact must end with one or more citation markers, e.g. [1] or [2][3].
3. Quote figures, limits, dates and names exactly as they appear. Do not round, convert or infer them.
4. If the sources do not contain enough to answer, reply with exactly this token and nothing else: {INSUFFICIENT}
5. If the sources answer only part of the question, answer that part, cite it, and state plainly which part is not covered.
6. If the question names a specific plan, tier, product, programme or other entity, first check that the sources actually define an entity by that name. If they do not, reply with {INSUFFICIENT} - do not answer about a similarly-named entity, and do not answer about a generic default. A source sentence that merely contains the word is not a definition.
7. A source marked SUPERSEDED is not current policy. Never mix its figures with figures from a CURRENT source. If you cite one, say which period it applied to.
8. Be concise: two to five sentences unless the question needs a list or a comparison table.
9. Do not repeat the question or add a preamble."""

BASELINE_ANSWER_SYSTEM = """You are a helpful assistant. Answer the user's question using the numbered context passages below. Cite the passages you used with markers like [1].

Answer in a few sentences."""
"""The ``baseline`` profile's prompt.

Deliberately missing everything that makes the improved prompt safe: no
refusal token, no instruction to quote figures exactly, no handling of
superseded documents, no rule about partial answers.  It still asks for
citations, so that citation-accuracy stays comparable between the two profiles
-- the interesting result is that the baseline cites confidently while being
wrong, which is the failure users actually report.
"""

CLARIFY_SYSTEM = """You write one short clarifying question for an internal knowledge assistant.

You are given a vague employee question and the distinct topics the search engine matched. Ask which of those topics they mean.

Rules:
- One sentence, then a short bulleted list of the concrete options.
- Use only the topics provided. Do not invent options.
- Do not attempt to answer the original question."""

VERIFY_SYSTEM = """You check whether an answer is supported by its sources.

For each factual claim in the answer, decide whether the cited sources state it.

Return JSON only:
{"claims": [{"claim": "<short>", "supported": true|false, "cited": [<source numbers>]}], "verdict": "grounded"|"partially_grounded"|"unsupported"}

Be strict. A claim is supported only if a source states it. A claim that is merely plausible, or that generalises beyond what the source says, is NOT supported."""


def format_source(index: int, hit: Hit) -> str:
    chunk: Chunk = hit.chunk
    status = "CURRENT" if chunk.is_current else "SUPERSEDED"
    descriptor_parts = [chunk.department]
    if chunk.effective_date:
        descriptor_parts.append(f"effective {chunk.effective_date}")
    if chunk.version:
        descriptor_parts.append(f"v{chunk.version}")
    descriptor_parts.append(status)
    descriptor = ", ".join(descriptor_parts)

    header = f"[{index}] {chunk.title} > {chunk.section_path} ({descriptor})"
    return f"{header}\n{chunk.text}"


def build_context(hits: list[Hit], max_chars: int) -> tuple[str, list[Hit]]:
    """Assemble the numbered source block, respecting a character budget.

    Returns the block and the hits that actually fit, so citation numbers in
    the prompt always resolve to something the caller still has.
    """
    blocks: list[str] = []
    used: list[Hit] = []
    total = 0

    for index, hit in enumerate(hits, start=1):
        block = format_source(index, hit)
        if total + len(block) > max_chars and used:
            break
        blocks.append(block)
        used.append(hit)
        total += len(block)

    return "\n\n".join(blocks), used


def build_answer_messages(
    question: str,
    context: str,
    history: list[Turn] | None = None,
) -> list[dict[str, str]]:
    messages = [{"role": "system", "content": ANSWER_SYSTEM}]

    # Recent turns are included for tone and pronoun resolution only; retrieval
    # already happened against the condensed standalone question, so history
    # cannot pull irrelevant documents into the answer.
    for turn in (history or [])[-4:]:
        messages.append({"role": turn.role, "content": turn.content[:500]})

    messages.append(
        {
            "role": "user",
            "content": f"Sources:\n\n{context}\n\nQuestion: {question}\n\nAnswer:",
        }
    )
    return messages


def build_clarify_messages(question: str, options: list[str]) -> list[dict[str, str]]:
    listed = "\n".join(f"- {option}" for option in options)
    return [
        {"role": "system", "content": CLARIFY_SYSTEM},
        {"role": "user", "content": f"Question: {question}\n\nMatched topics:\n{listed}"},
    ]


def build_verify_messages(answer: str, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": VERIFY_SYSTEM},
        {"role": "user", "content": f"Sources:\n\n{context}\n\nAnswer:\n{answer}"},
    ]
