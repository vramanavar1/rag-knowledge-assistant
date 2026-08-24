"""Local hybrid store: BM25 + dense vectors fused with Reciprocal Rank Fusion.

This is the default backend.  It exists so the whole pipeline -- including the
evaluation harness -- is runnable and reproducible with no cloud dependency,
and so retrieval behaviour can be reasoned about without a network round trip.
It implements the same contract as the Azure AI Search backend, including
in-query filtering, so switching between them changes latency and scale, not
semantics.

Why hybrid rather than pure vector
----------------------------------
This corpus is full of exact tokens that embeddings blur: "$350", "99.9%",
"Tier 2", "FIDO2", "Net 30", "text-embedding". Dense retrieval is good at
"what do I get for parental leave" and bad at "what is the 2-Year prepaid
discount"; BM25 is the reverse. RRF fuses the two rankings without needing the
scores to be on a comparable scale, which is exactly the property that makes it
robust when one of the two retrievers is weak -- including when the local
embedder is standing in for a real embedding model.

Async, but CPU-bound
--------------------
This store satisfies an async ``SearchBackend`` protocol while doing no network
I/O at all: scoring is arithmetic over in-memory dicts.  That combination is a
trap.  An ``async def`` that runs CPU work inline holds the event loop for its
whole duration, so a single search would stall every other in-flight request --
strictly worse than the threadpool it replaced.

So the scoring bodies stay synchronous and are dispatched with
``asyncio.to_thread``.  The ``threading.Lock`` around ``_rebuild`` is what makes
that safe, and matters more now than it did before: the work really does run on
worker threads.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from rag.models import Chunk, Hit
from rag.observability.tracing import get_logger
from rag.store.base import (
    MODE_HYBRID,
    MODE_KEYWORD,
    MODE_VECTOR,
    IndexStats,
    SearchFilters,
)
from rag.text import analyze, tokenize  # noqa: F401  (re-exported for callers)

log = get_logger(__name__)

try:  # optional acceleration
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is optional
    _np = None

BM25_K1 = 1.2
BM25_B = 0.75
RRF_K = 60


class LocalHybridStore:
    """In-memory index with JSON persistence."""

    name = "local"

    def __init__(self, path: Path, *, profile: str = "improved") -> None:
        self._path = path
        self._profile = profile
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}
        self._dimensions = 0
        self._embedding_provider = ""

        # BM25 state, rebuilt whenever the corpus changes.
        self._tokens: dict[str, list[str]] = {}
        self._term_freq: dict[str, Counter] = {}
        self._doc_freq: Counter = Counter()
        self._avg_len = 0.0
        self._dirty_index = True
        # Sub-query searches now run concurrently, so two threads can
        # reach _ensure_fresh() at once. Rebuilding twice in parallel
        # would corrupt the term-frequency maps mid-write.
        self._rebuild_lock = threading.Lock()

        # numpy fast path
        self._matrix: Any = None
        self._matrix_ids: list[str] = []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def load_sync(self) -> bool:
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("index unreadable, starting empty", path=str(self._path),
                        error=str(exc))
            return False

        self._dimensions = data.get("dimensions", 0)
        self._embedding_provider = data.get("embedding_provider", "")
        for record in data.get("chunks", []):
            chunk = Chunk.from_dict(record["chunk"])
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = record.get("vector") or []
        self._dirty_index = True
        log.info("index loaded", path=str(self._path), chunks=len(self._chunks),
                 provider=self._embedding_provider)
        return True

    async def load(self) -> bool:
        return await asyncio.to_thread(self.load_sync)

    def _save_sync(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profile": self._profile,
            "dimensions": self._dimensions,
            "embedding_provider": self._embedding_provider,
            "chunks": [
                {"chunk": chunk.to_dict(), "vector": self._vectors.get(chunk_id, [])}
                for chunk_id, chunk in self._chunks.items()
            ],
        }
        self._path.write_text(json.dumps(payload), encoding="utf-8")
        log.info("index saved", path=str(self._path), chunks=len(self._chunks))

    async def save(self) -> None:
        # Serialising every chunk and vector to JSON is CPU plus a blocking
        # write; both belong off the loop.
        await asyncio.to_thread(self._save_sync)

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def ensure_index(self, dimensions: int) -> None:
        if self._dimensions and self._dimensions != dimensions:
            # Mixing vector widths silently produces meaningless similarities.
            log.warning(
                "embedding dimension changed; existing vectors are incompatible "
                "and will be dropped",
                previous=self._dimensions,
                current=dimensions,
            )
            self._vectors.clear()
        self._dimensions = dimensions

    def set_embedding_provider(self, name: str) -> None:
        self._embedding_provider = name

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        for chunk, vector in zip(chunks, vectors):
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = _normalize(vector)
        self._dirty_index = True
        return len(chunks)

    async def delete_by_doc(self, doc_id: str) -> int:
        victims = [cid for cid, c in self._chunks.items() if c.doc_id == doc_id]
        for chunk_id in victims:
            self._chunks.pop(chunk_id, None)
            self._vectors.pop(chunk_id, None)
        if victims:
            self._dirty_index = True
        return len(victims)

    def delete_chunks(self, chunk_ids: Iterable[str]) -> int:
        removed = 0
        for chunk_id in chunk_ids:
            if self._chunks.pop(chunk_id, None) is not None:
                self._vectors.pop(chunk_id, None)
                removed += 1
        if removed:
            self._dirty_index = True
        return removed

    async def patch_document_fields(self, doc_id: str, fields: dict[str, Any]) -> int:
        patched = 0
        for chunk in self._chunks.values():
            if chunk.doc_id != doc_id:
                continue
            for key, value in fields.items():
                if hasattr(chunk, key):
                    setattr(chunk, key, value)
            patched += 1
        return patched

    # ------------------------------------------------------------------
    # Index maintenance
    # ------------------------------------------------------------------

    def _rebuild(self) -> None:
        self._tokens.clear()
        self._term_freq.clear()
        self._doc_freq = Counter()

        for chunk_id, chunk in self._chunks.items():
            tokens = analyze(chunk.embed_text)
            self._tokens[chunk_id] = tokens
            counts = Counter(tokens)
            self._term_freq[chunk_id] = counts
            self._doc_freq.update(counts.keys())

        lengths = [len(t) for t in self._tokens.values()]
        self._avg_len = (sum(lengths) / len(lengths)) if lengths else 0.0

        if _np is not None and self._vectors:
            ids = [cid for cid in self._chunks if self._vectors.get(cid)]
            if ids:
                self._matrix_ids = ids
                self._matrix = _np.array([self._vectors[cid] for cid in ids],
                                         dtype=_np.float32)
            else:
                self._matrix, self._matrix_ids = None, []

        self._dirty_index = False

    def _ensure_fresh(self) -> None:
        if not self._dirty_index:
            return
        with self._rebuild_lock:
            if self._dirty_index:      # re-check: another thread may have won
                self._rebuild()

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    def _bm25(self, query: str, allowed: list[str]) -> dict[str, float]:
        query_terms = analyze(query)
        if not query_terms or not allowed:
            return {}

        total = len(self._chunks) or 1
        scores: dict[str, float] = {}

        idf = {}
        for term in set(query_terms):
            df = self._doc_freq.get(term, 0)
            idf[term] = math.log(1 + (total - df + 0.5) / (df + 0.5))

        for chunk_id in allowed:
            counts = self._term_freq.get(chunk_id)
            if not counts:
                continue
            length = len(self._tokens.get(chunk_id, ()))
            norm = BM25_K1 * (1 - BM25_B + BM25_B * length / (self._avg_len or 1))
            score = 0.0
            for term in query_terms:
                freq = counts.get(term, 0)
                if freq:
                    score += idf[term] * freq * (BM25_K1 + 1) / (freq + norm)
            if score > 0:
                scores[chunk_id] = score
        return scores

    def _vector_scores(self, vector: list[float], allowed: list[str]) -> dict[str, float]:
        if not vector or not allowed:
            return {}
        query = _normalize(vector)
        allowed_set = set(allowed)

        if _np is not None and self._matrix is not None:
            similarities = self._matrix @ _np.array(query, dtype=_np.float32)
            return {
                chunk_id: float(score)
                for chunk_id, score in zip(self._matrix_ids, similarities)
                if chunk_id in allowed_set
            }

        scores: dict[str, float] = {}
        for chunk_id in allowed:
            stored = self._vectors.get(chunk_id)
            if not stored or len(stored) != len(query):
                continue
            scores[chunk_id] = sum(a * b for a, b in zip(stored, query))
        return scores

    async def search(
        self,
        query: str,
        vector: list[float] | None,
        filters: SearchFilters,
        top_k: int,
        mode: str = MODE_HYBRID,
    ) -> list[Hit]:
        return await asyncio.to_thread(
            self._search_sync, query, vector, filters, top_k, mode
        )

    def _search_sync(
        self,
        query: str,
        vector: list[float] | None,
        filters: SearchFilters,
        top_k: int,
        mode: str = MODE_HYBRID,
    ) -> list[Hit]:
        self._ensure_fresh()

        allowed = [cid for cid, chunk in self._chunks.items() if filters.allows(chunk)]
        if not allowed:
            return []

        keyword = self._bm25(query, allowed) if mode in (MODE_HYBRID, MODE_KEYWORD) else {}
        dense = (
            self._vector_scores(vector or [], allowed)
            if mode in (MODE_HYBRID, MODE_VECTOR)
            else {}
        )

        if mode == MODE_KEYWORD:
            ranked = sorted(keyword.items(), key=lambda kv: -kv[1])[:top_k]
            return [
                Hit(chunk=self._chunks[cid], score=score, keyword_score=score)
                for cid, score in ranked
            ]

        if mode == MODE_VECTOR:
            ranked = sorted(dense.items(), key=lambda kv: -kv[1])[:top_k]
            return [
                Hit(chunk=self._chunks[cid], score=score, vector_score=score)
                for cid, score in ranked
            ]

        # Hybrid: fuse the two rankings, not the two score scales.
        fused = _reciprocal_rank_fusion([keyword, dense])
        ranked = sorted(fused.items(), key=lambda kv: -kv[1])[:top_k]
        return [
            Hit(
                chunk=self._chunks[cid],
                score=score,
                rrf_score=score,
                keyword_score=keyword.get(cid),
                vector_score=dense.get(cid),
            )
            for cid, score in ranked
        ]

    # ------------------------------------------------------------------

    def _document_ids_sync(self) -> list[str]:
        return sorted({chunk.doc_id for chunk in self._chunks.values()})

    async def document_ids(self) -> list[str]:
        return self._document_ids_sync()

    async def aclose(self) -> None:
        """Nothing to release: the index is in memory and the file is closed."""

    def chunks_for_doc(self, doc_id: str) -> list[Chunk]:
        return sorted(
            (c for c in self._chunks.values() if c.doc_id == doc_id),
            key=lambda c: c.ordinal,
        )

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        return self._chunks.get(chunk_id)

    async def stats(self, *, full: bool = False) -> IndexStats:
        # Everything here is an in-memory dict, so `full` costs nothing
        # extra; the parameter exists to satisfy the protocol.
        return IndexStats(
            backend=self.name,
            chunks=len(self._chunks),
            documents=len(self._document_ids_sync()),
            dimensions=self._dimensions,
            embedding_provider=self._embedding_provider,
            profile=self._profile,
            extra={
                "numpy": _np is not None,
                "table_chunks": sum(
                    1 for c in self._chunks.values() if c.content_type == "table"
                ),
            },
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        return vector
    return [v / norm for v in vector]


def _reciprocal_rank_fusion(rankings: list[dict[str, float]],
                            k: int = RRF_K) -> dict[str, float]:
    """Fuse ranked lists by rank position rather than by score.

    RRF is used instead of a weighted score sum because BM25 scores are
    unbounded and cosine similarities are not, so any fixed weighting between
    them is a guess that breaks as soon as the corpus or the embedding model
    changes.
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        ordered = sorted(ranking.items(), key=lambda kv: -kv[1])
        for rank, (chunk_id, _score) in enumerate(ordered, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return fused
