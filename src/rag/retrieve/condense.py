"""Conversational context handling (assignment Scenario 6).

The failure this prevents
-------------------------
The naive approach to multi-turn RAG is to concatenate the conversation and
embed the lot.  Three turns in, the retrieval query is carrying the text of two
previous answers, and a question about the Starter tier retrieves chunks about
Enterprise support SLAs because those words are still in the query.  Retrieval
quality degrades monotonically with conversation length.

The approach here
-----------------
Rewrite each follow-up into a *standalone* question and retrieve with that
alone.  Two rules keep the rewrite from reintroducing the pollution:

1. Only the last few turns are considered at all.
2. Only entities are carried forward -- never the content of prior answers.
   "What about Standard?" becomes "What is the Standard plan cancellation
   policy?", not "What is the Standard plan cancellation policy, given that
   Enterprise requires 30 days written notice before renewal...".

Condensation is skipped entirely when the question is already self-contained,
which saves a model call on most turns and avoids the rewrite introducing
errors into questions that did not need it.
"""

from __future__ import annotations

import re

from rag.models import Turn
from rag.observability.tracing import get_logger
from rag.providers.llm import ChatProvider

log = get_logger(__name__)

MAX_HISTORY_TURNS = 6          # 3 exchanges

# Signals that a question cannot be understood on its own.
_PRONOUNS = re.compile(
    r"\b(it|its|it's|that|this|those|these|they|them|their|there|"
    r"one|ones|same|above|former|latter)\b",
    re.I,
)
_ELLIPTIC_OPENERS = re.compile(
    r"^\s*(and|but|so|what about|how about|whatabout|ok(?:ay)? but|also|"
    r"then|why|why not|is there|are there|any|what if|which one)\b",
    re.I,
)
_WHAT_ABOUT = re.compile(r"^\s*(?:and\s+)?(?:what|how)\s+about\s+(?:the\s+)?(.+?)\s*[?.!]*\s*$", re.I)

_CONDENSE_SYSTEM = """You rewrite follow-up questions into standalone questions for a document search engine.

Rules:
- Output ONLY the rewritten question. No preamble, no explanation, no quotes.
- Carry forward entities and topic words from earlier turns ONLY when the follow-up is incomplete without them.
- Never copy facts, numbers, or wording from previous ANSWERS into the question. Use the earlier QUESTIONS for context.
- Keep it to one sentence and preserve the user's intent exactly. Do not add constraints they did not state.
- If the follow-up is already a complete, standalone question, return it unchanged."""


def needs_condensation(query: str, history: list[Turn]) -> bool:
    if not history:
        return False
    stripped = query.strip()
    if _ELLIPTIC_OPENERS.match(stripped):
        return True
    if _PRONOUNS.search(stripped):
        return True
    # Very short questions are almost always elliptical in a conversation.
    return len(stripped.split()) <= 5


def _heuristic_condense(query: str, history: list[Turn]) -> str:
    """Rule-based rewrite used when no chat model is available.

    Handles the dominant "What about X?" shape by substituting X for the
    parallel entity in the previous question; otherwise it scopes the follow-up
    to the previous question rather than concatenating both, which at least
    keeps prior answers out of the retrieval query.
    """
    previous = next(
        (t.content for t in reversed(history) if t.role == "user"), ""
    )
    if not previous:
        return query

    match = _WHAT_ABOUT.match(query)
    if match:
        replacement = match.group(1).strip().rstrip("?.!")
        # Substitute for the first proper-noun-ish token of the earlier question.
        tokens = previous.split()
        for index, token in enumerate(tokens):
            bare = token.strip(".,?!'\"")
            if index > 0 and bare[:1].isupper() and bare.lower() not in {"i"}:
                tokens[index] = token.replace(bare, replacement)
                return " ".join(tokens)
        return f"{replacement}: {previous}"

    return f"{query.strip().rstrip('?')} (regarding: {previous.strip().rstrip('?')})?"


def condense_query(
    query: str,
    history: list[Turn],
    llm: ChatProvider,
) -> tuple[str, bool]:
    """Return ``(standalone_query, was_rewritten)``."""
    if not needs_condensation(query, history):
        return query.strip(), False

    recent = history[-MAX_HISTORY_TURNS:]

    if llm.available:
        transcript = "\n".join(
            f"{'User' if turn.role == 'user' else 'Assistant'}: {turn.content[:400]}"
            for turn in recent
        )
        result = llm.complete(
            [
                {"role": "system", "content": _CONDENSE_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Conversation so far:\n{transcript}\n\n"
                        f"Follow-up question: {query}\n\nStandalone question:"
                    ),
                },
            ],
            temperature=0.0,
            max_tokens=120,
            utility=True,
        )
        if result and result.text.strip():
            rewritten = result.text.strip().strip('"').splitlines()[0]
            log.info("query condensed", original=query, standalone=rewritten)
            return rewritten, True

    rewritten = _heuristic_condense(query, recent)
    log.info("query condensed (heuristic)", original=query, standalone=rewritten)
    return rewritten, True
