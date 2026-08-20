"""The retrieval pipeline.

    query + history
      -> condense to a standalone question           (Scenario 6)
      -> decompose into sub-queries if multi-hop     (Scenario 2)
      -> security filter, applied inside the query   (Step 5, Q4)
      -> hybrid retrieve per sub-query, fuse by RRF  (Scenario 1)
      -> rerank the candidate set                    (Scenario 1, Step 5 Q1)
      -> version-aware re-ranking                    (Scenario 3)
      -> select context, balanced across sub-queries (Scenario 2)

The ``baseline`` profile skips every one of those stages and does a single
top-5 vector search, which is the "before" system the evaluation compares
against.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rag.config import Settings
from rag.models import Hit, Principal, Turn
from rag.observability.tracing import get_logger, stage
from rag.providers.embeddings import EmbeddingProvider
from rag.providers.llm import ChatProvider
from rag.retrieve.condense import condense_query
from rag.retrieve.decompose import decompose_query
from rag.retrieve.recency import apply_version_ranking, prefilter_superseded
from rag.retrieve.rerank import rerank
from rag.store.base import MODE_HYBRID, MODE_VECTOR, SearchBackend, SearchFilters

log = get_logger(__name__)


@dataclass
class RetrievalOutcome:
    hits: list[Hit] = field(default_factory=list)
    candidates: list[Hit] = field(default_factory=list)
    standalone_query: str = ""
    subqueries: list[str] = field(default_factory=list)
    condensed: bool = False
    rerank_method: str = "none"
    trace: dict[str, Any] = field(default_factory=dict)

    @property
    def top_score(self) -> float:
        if not self.hits:
            return 0.0
        best = self.hits[0]
        return best.rerank_score if best.rerank_score is not None else best.score


class Retriever:
    def __init__(
        self,
        settings: Settings,
        backend: SearchBackend,
        embedder: EmbeddingProvider,
        llm: ChatProvider,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._embedder = embedder
        self._llm = llm

    # ------------------------------------------------------------------

    def _filters_for(self, principal: Principal | None) -> SearchFilters:
        """Security trimming.

        Applied as part of the query, never as a post-filter: post-filtering
        lets documents the caller cannot read consume top-k slots, which
        degrades their results and leaks the existence of those documents
        through the shape of the response.
        """
        if principal is None or "*" in principal.departments:
            return SearchFilters()
        return SearchFilters(departments=list(principal.departments))

    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        history: list[Turn] | None = None,
        principal: Principal | None = None,
    ) -> RetrievalOutcome:
        if self._settings.is_baseline:
            return self._retrieve_baseline(query, principal)
        return self._retrieve_improved(query, history or [], principal)

    # ------------------------------------------------------------------

    def _retrieve_baseline(
        self, query: str, principal: Principal | None
    ) -> RetrievalOutcome:
        """Single-shot top-k vector search. No rewriting, ranking or filtering.

        Note it does not even apply the security filter: forgetting access
        control is part of what a first-pass implementation looks like, and the
        evaluation reports it as a finding rather than hiding it.
        """
        with stage("embed") as st:
            vector = self._embedder.embed([query])[0]
            st["vectors"] = 1
            st["dimensions"] = self._embedder.dimensions
            st["provider"] = self._embedder.name

        with stage("search") as st:
            hits = self._backend.search(
                query,
                vector,
                SearchFilters(),
                self._settings.baseline_top_k,
                mode=MODE_VECTOR,
            )
            st["hits"] = len(hits)

        return RetrievalOutcome(
            hits=hits,
            candidates=hits,
            standalone_query=query,
            subqueries=[query],
            rerank_method="none",
            trace={"profile": "baseline", "mode": MODE_VECTOR,
                   "security_filter": False},
        )

    # ------------------------------------------------------------------

    def _retrieve_improved(
        self,
        query: str,
        history: list[Turn],
        principal: Principal | None,
    ) -> RetrievalOutcome:
        trace: dict[str, Any] = {"profile": "improved", "mode": MODE_HYBRID}

        with stage("condense") as st:
            standalone, condensed = condense_query(query, history, self._llm)
            st["rewritten"] = condensed

        with stage("decompose") as st:
            subqueries = decompose_query(standalone, self._llm)
            st["subqueries"] = len(subqueries)

        filters = self._filters_for(principal)
        trace["security_filter"] = filters.departments
        trace["condensed"] = condensed

        # Embedding is timed separately from search rather than folded into it:
        # they are different stages of the assignment's pipeline, they fail for
        # different reasons, and a latency regression in one says nothing about
        # the other.
        with stage("embed") as st:
            vectors = self._embedder.embed(subqueries)
            st["vectors"] = len(vectors)
            st["dimensions"] = self._embedder.dimensions
            st["provider"] = self._embedder.name

        with stage("search") as st:
            merged: dict[str, Hit] = {}

            for subquery, vector in zip(subqueries, vectors):
                found = self._backend.search(
                    subquery,
                    vector,
                    filters,
                    self._settings.retrieve_top_k,
                    mode=MODE_HYBRID,
                )
                for hit in found:
                    existing = merged.get(hit.chunk.chunk_id)
                    if existing is None or hit.score > existing.score:
                        hit.matched_subquery = subquery
                        merged[hit.chunk.chunk_id] = hit

            candidates = sorted(merged.values(), key=lambda h: -h.score)
            st["candidates"] = len(candidates)

        if not candidates:
            return RetrievalOutcome(
                standalone_query=standalone,
                subqueries=subqueries,
                condensed=condensed,
                trace=trace | {"reason": "no candidates passed the filter"},
            )

        # Superseded duplicates are removed BEFORE reranking, so the reranker
        # never spends a candidate slot -- or its confidence -- on a chunk that
        # is about to be discarded. See recency.prefilter_superseded.
        with stage("version_filter") as st:
            candidates, filter_trace = prefilter_superseded(candidates, standalone)
            st.update(filter_trace)

        with stage("rerank") as st:
            # The reranker only scores its first MAX_CANDIDATES. With three
            # sub-queries the merged list can be 60 long, and a chunk that is
            # rank 1 for the third sub-query but rank 25 by fused score would
            # never be scored at all -- which would quietly undo the balanced
            # context selection further down. Interleaving first guarantees
            # every sub-query reaches the rerank window.
            candidates = self._interleave_by_subquery(
                candidates, subqueries, len(candidates)
            )
            candidates, method = rerank(standalone, candidates, self._llm)
            st["method"] = method
            trace["rerank_method"] = method

        with stage("version_rank") as st:
            ranked, version_trace = apply_version_ranking(candidates, standalone)
            trace["versioning"] = filter_trace | version_trace
            st.update(version_trace)

        selected = self._interleave_by_subquery(
            ranked, subqueries, self._settings.context_top_k
        )
        trace["selected"] = len(selected)

        return RetrievalOutcome(
            hits=selected,
            candidates=ranked,
            standalone_query=standalone,
            subqueries=subqueries,
            condensed=condensed,
            rerank_method=method,
            trace=trace,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _interleave_by_subquery(
        hits: list[Hit], subqueries: list[str], k: int
    ) -> list[Hit]:
        """Round-robin across sub-queries, preserving order within each.

        Taking a global top-k after a decomposition defeats the point: for
        "compare Enterprise and Starter", the Enterprise chunks routinely score
        higher across the board and would take every slot, leaving the model to
        answer half the question or invent the other half.  Round-robin
        guarantees each sub-query contributes before any sub-query contributes
        twice.

        Used in two places -- to build the rerank window, and to fill the
        context window -- because a fair share of either is useless without a
        fair share of the other.
        """
        if len(subqueries) <= 1:
            return hits[:k]

        buckets: dict[str, list[Hit]] = {sq: [] for sq in subqueries}
        for hit in hits:
            buckets.setdefault(hit.matched_subquery or subqueries[0], []).append(hit)

        selected: list[Hit] = []
        seen: set[str] = set()
        depth = 0
        while len(selected) < k:
            progressed = False
            for subquery in subqueries:
                bucket = buckets.get(subquery, [])
                if depth < len(bucket):
                    hit = bucket[depth]
                    progressed = True
                    if hit.chunk.chunk_id not in seen:
                        seen.add(hit.chunk.chunk_id)
                        selected.append(hit)
                        if len(selected) >= k:
                            break
            if not progressed:
                break
            depth += 1

        return selected
