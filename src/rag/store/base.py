"""The ``SearchBackend`` seam.

Everything above this line (retrieval pipeline, generation, API) is written
against this protocol only, which is what lets the same application run on a
local index during development and on Azure AI Search in production without a
branch anywhere in the pipeline code.

The protocol is deliberately small.  Note two operations that a naive design
would omit and that the document lifecycle depends on:

``delete_by_doc``      -- a modified document must have its previous chunks
                          removed before the new ones land, or shrinking a
                          document leaves orphan chunks that still get
                          retrieved and cited.
``patch_document_fields`` -- when a new rate card supersedes an old one, the
                          old document's chunks are unchanged but their
                          ``is_current`` flag is not.  Patching beats
                          re-embedding an untouched document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from rag.models import Chunk, Hit

# Retrieval modes, exposed so the evaluation harness can ablate them.
MODE_HYBRID = "hybrid"
MODE_VECTOR = "vector"
MODE_KEYWORD = "keyword"


@dataclass
class SearchFilters:
    """Pre-filters applied inside the query, never after it.

    Security trimming has to happen *within* retrieval: filtering results
    afterwards means an HR chunk can consume one of the top-k slots that a
    Sales user's answer needed, degrading their results and leaking the
    existence of documents they cannot read.
    """

    departments: list[str] | None = None      # None => unrestricted
    doc_ids: list[str] | None = None
    exclude_doc_ids: list[str] | None = None
    current_only: bool = False
    content_types: list[str] | None = None

    def allows(self, chunk: Chunk) -> bool:
        if self.departments is not None and chunk.department not in self.departments:
            return False
        if self.doc_ids is not None and chunk.doc_id not in self.doc_ids:
            return False
        if self.exclude_doc_ids and chunk.doc_id in self.exclude_doc_ids:
            return False
        if self.current_only and not chunk.is_current:
            return False
        if self.content_types is not None and chunk.content_type not in self.content_types:
            return False
        return True


@dataclass
class IndexStats:
    backend: str
    chunks: int = 0
    documents: int = 0
    # False when `documents` is a lower bound rather than a count. Aggregating
    # distinct document ids over a large index is expensive and capped, so the
    # cheap path does not attempt it -- and says so rather than reporting a
    # number that quietly stops being true past the cap.
    documents_exact: bool = True
    dimensions: int = 0
    embedding_provider: str = ""
    profile: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SearchBackend(Protocol):
    """Every operation is ``async``.

    Not because both implementations need it -- the local store is in-memory --
    but because one of them talks to a remote service over the network, and the
    protocol has to be shaped for the slower of the two.  A sync protocol would
    force the Azure backend to block a thread on every call, which is the whole
    problem this seam exists to avoid.

    An implementation whose work is CPU-bound rather than network-bound is still
    obliged not to hold the event loop: see ``LocalHybridStore``, which does its
    scoring in a worker thread.
    """

    name: str

    async def ensure_index(self, dimensions: int) -> None:
        """Create or update the index definition. Idempotent."""

    async def vector_width(self) -> int:
        """Width of the vectors *actually stored*, or 0 when unknown.

        Deliberately not derived from configuration: the whole point is to
        detect the case where the index was built by a different embedder than
        the one now querying it, and a configured value cannot see that.

        0 means "no opinion" -- an empty index, or one that does not exist yet.
        Callers must treat that as "cannot check", never as a mismatch.
        """

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        """Insert or replace chunks by ``chunk_id``. Idempotent."""

    async def delete_by_doc(self, doc_id: str) -> int:
        """Remove every chunk belonging to a document. Returns the count."""

    async def patch_document_fields(self, doc_id: str, fields: dict[str, Any]) -> int:
        """Update denormalised document metadata on a document's chunks."""

    async def search(
        self,
        query: str,
        vector: list[float] | None,
        filters: SearchFilters,
        top_k: int,
        mode: str = MODE_HYBRID,
    ) -> list[Hit]:
        """Retrieve candidates. ``filters`` are applied inside the query."""

    async def document_ids(self) -> list[str]:
        ...

    async def stats(self, *, full: bool = False) -> IndexStats:
        """Index statistics.

        ``full=False`` (the default) must be cheap enough for a readiness probe
        polled every few seconds: chunk count only, no aggregation. ``full=True``
        may run expensive aggregates and is for ingest reporting and startup.
        """

    async def save(self) -> None:
        """Persist, where the backend is persistent. No-op for remote stores."""

    async def aclose(self) -> None:
        """Release connection pools. No-op for in-memory stores."""
