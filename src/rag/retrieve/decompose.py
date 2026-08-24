"""Multi-hop query decomposition (assignment Scenario 2).

The failure this prevents
-------------------------
"Compare the Enterprise and Starter pricing" produces one embedding that sits
in the semantic space *between* the two tiers, and top-k comes back with five
chunks that are each half-relevant.  Worse, when the two facts live in
different documents -- "what is my hotel cap in Chicago and my dinner per
diem", which spans two sections of the travel policy, or "what discount for 150
seats on an annual contract and who signs it off", which spans three tabs of
the discount workbook -- a single query cannot rank both sources highly at
once, because the terms that pull one up push the other down.

The approach here
-----------------
Detect that a question needs more than one lookup, split it into independent
sub-queries, retrieve for each, and union the results.  Each retrieved chunk
remembers which sub-query found it, so the answer builder can tell whether
every part of a comparison was actually evidenced -- and say so when one side
is missing rather than quietly answering half the question.
"""

from __future__ import annotations

import re

from rag.observability.tracing import get_logger
from rag.providers.llm import ChatProvider

log = get_logger(__name__)

MAX_SUBQUERIES = 3

_COMPARATIVE = re.compile(
    r"\b(compare|comparison|versus|vs\.?|difference(?:s)? between|"
    r"both|each of|as well as|relative to)\b",
    re.I,
)
_DIFF_BETWEEN = re.compile(r"difference(?:s)? between (.+?) and (.+?)[?.]?$", re.I)
_VERSUS = re.compile(r"^(.*?)\b(?:vs\.?|versus)\b(.*)$", re.I)
_COMPARE_ASPECT = re.compile(
    r"compare (?:the )?(.+?) (?:for|of|between) (.+?) and (.+?)[?.]?$", re.I
)
# "X and Y" where both sides look like separate askable things.
_AND_SPLIT = re.compile(r"\band\b", re.I)

_DECOMPOSE_SYSTEM = """You split a question into the minimum set of independent document-search queries needed to answer it.

Rules:
- Return JSON only: {"subqueries": ["...", "..."]}
- Return ONE subquery if a single search can answer the question. Do not split unnecessarily.
- Return at most 3.
- Each subquery must be a complete, standalone search query that makes sense on its own.
- Preserve the user's exact entities and qualifiers (tiers, cities, years, seat counts) in the subquery they belong to."""


def looks_multi_hop(query: str) -> bool:
    if _COMPARATIVE.search(query):
        return True
    # "A and B" with enough substance on both sides to be two lookups.
    parts = _AND_SPLIT.split(query)
    if len(parts) == 2 and all(len(p.split()) >= 3 for p in parts):
        return len(query.split()) >= 9
    return False


def _heuristic_decompose(query: str) -> list[str]:
    cleaned = query.strip().rstrip("?")

    if m := _COMPARE_ASPECT.search(cleaned):
        aspect, left, right = (g.strip() for g in m.groups())
        return [f"{aspect} for {left}", f"{aspect} for {right}"]

    if m := _DIFF_BETWEEN.search(cleaned):
        left, right = (g.strip() for g in m.groups())
        return [left, right]

    if m := _VERSUS.match(cleaned):
        left, right = m.group(1).strip(), m.group(2).strip()
        if left and right:
            # Carry the leading interrogative onto the bare right-hand side.
            lead = re.match(r"^((?:what|which|how)\b.*?\b(?:is|are|for)\b)\s*", left, re.I)
            prefix = lead.group(1) + " " if lead else ""
            return [left, f"{prefix}{right}".strip()]

    parts = [p.strip() for p in _AND_SPLIT.split(cleaned) if p.strip()]
    if len(parts) == 2 and all(len(p.split()) >= 3 for p in parts):
        # Give the second clause the interrogative of the first.
        lead = re.match(r"^((?:what|which|how|when|who)\b[^,]{0,24}?\b(?:is|are|do|does)\b)",
                        parts[0], re.I)
        prefix = lead.group(1) + " " if lead else ""
        return [parts[0], f"{prefix}{parts[1]}".strip()]

    return [cleaned]


async def decompose_query(query: str, llm: ChatProvider) -> list[str]:
    """Return 1..3 sub-queries. A single-element list means no decomposition."""
    if not looks_multi_hop(query):
        return [query.strip()]

    if llm.available:
        result = await llm.complete(
            [
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": query},
            ],
            temperature=0.0,
            max_tokens=200,
            json_mode=True,
            utility=True,
        )
        parsed = result.json() if result else None
        if isinstance(parsed, dict):
            subqueries = [
                str(s).strip()
                for s in parsed.get("subqueries", [])
                if str(s).strip()
            ][:MAX_SUBQUERIES]
            if subqueries:
                if len(subqueries) > 1:
                    log.info("query decomposed", query=query, subqueries=subqueries)
                return subqueries

    subqueries = _heuristic_decompose(query)[:MAX_SUBQUERIES]
    if len(subqueries) > 1:
        log.info("query decomposed (heuristic)", query=query, subqueries=subqueries)
    return subqueries
