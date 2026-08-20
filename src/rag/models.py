"""Core data structures shared by ingestion, retrieval and generation.

Plain dataclasses rather than pydantic models: these are persisted to JSON on
every ingest run, and dataclasses keep that round-trip explicit and cheap.
Pydantic is used only at the API boundary (see api/schemas.py).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------


@dataclass
class DocumentMeta:
    """Everything we know about a source document, independent of its chunks."""

    doc_id: str                      # stable id derived from the source-relative path
    path: str                        # source-relative path, forward slashes
    title: str
    department: str                  # Finance | HR | IT | Legal | Sales
    doc_type: str                    # policy | guide | rate_card | template | schedule
    content_hash: str                # sha256 of the raw file bytes
    source_ext: str                  # .pdf | .docx | .xlsx

    version: str | None = None
    effective_date: str | None = None       # ISO-8601 date; None if the doc states none
    effective_until: str | None = None
    supersedes_raw: str | None = None       # verbatim "Supersedes: ..." text
    supersedes_doc_id: str | None = None    # resolved during reconciliation
    superseded_by: str | None = None        # resolved during reconciliation
    is_current: bool = True

    page_count: int | None = None
    ingested_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DocumentMeta":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------


@dataclass
class Chunk:
    """One retrievable unit.

    ``text`` is what gets shown to the user and placed in the prompt.
    ``embed_text`` is what gets embedded and keyword-indexed: it carries a
    ``Title > Section`` breadcrumb (and the effective date) so that a chunk
    stays interpretable when it is retrieved without its neighbours.  That
    split is the single highest-leverage retrieval fix in this codebase --
    see docs/failure-scenarios.md, Scenario 1.
    """

    chunk_id: str
    doc_id: str
    ordinal: int
    section_path: str                # e.g. "3. Expense Categories & Limits"
    content_type: str                # "text" | "table"
    text: str
    embed_text: str

    page: int | None = None
    token_estimate: int = 0

    # Denormalised document metadata so the store can filter/rank without a join.
    title: str = ""
    department: str = ""
    doc_type: str = ""
    version: str | None = None
    effective_date: str | None = None
    is_current: bool = True
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chunk":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def citation_label(self) -> str:
        """Short human-readable handle used in citations, e.g. 'TravelPolicy §4'."""
        section = self.section_path.split(">")[-1].strip() or "document"
        return f"{self.title} §{section}" if section else self.title


def make_chunk_id(doc_id: str, section_path: str, ordinal: int) -> str:
    """Deterministic chunk id.

    Same document + same section + same position => same id, so re-ingesting
    is idempotent and an interrupted run converges on retry.
    """
    raw = f"{doc_id}|{section_path}|{ordinal}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:24]


def content_fingerprint(text: str) -> str:
    """Key for the embedding cache: identical text is never embedded twice."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------


@dataclass
class Hit:
    """A retrieved chunk plus every score that contributed to its rank.

    Keeping the individual signals (rather than a single fused number) is what
    makes the "why was this chunk returned?" panel in the UI possible, and it
    is how retrieval regressions get diagnosed -- assignment Step 5, Q1 and Q6.
    """

    chunk: Chunk
    score: float = 0.0               # final score after all stages
    vector_score: float | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    rerank_score: float | None = None
    recency_boost: float = 0.0
    matched_subquery: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["chunk"] = self.chunk.to_dict()
        return d


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


@dataclass
class Citation:
    marker: str                      # the "[1]" the model writes inline
    doc_id: str
    chunk_id: str
    title: str
    section_path: str
    page: int | None = None
    source_path: str = ""
    quote: str = ""                  # supporting snippet from the cited chunk


@dataclass
class Answer:
    """The full result of one turn, including everything needed to audit it."""

    text: str
    status: str                      # answered | insufficient_evidence | needs_clarification | denied
    citations: list[Citation] = field(default_factory=list)
    confidence: float = 0.0          # 0.0 - 1.0
    hits: list[Hit] = field(default_factory=list)
    standalone_query: str = ""
    subqueries: list[str] = field(default_factory=list)
    clarification_options: list[str] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "citations": [asdict(c) for c in self.citations],
            "confidence": round(self.confidence, 3),
            "standalone_query": self.standalone_query,
            "subqueries": self.subqueries,
            "clarification_options": self.clarification_options,
            "hits": [h.to_dict() for h in self.hits],
            "trace": self.trace,
        }


@dataclass
class Turn:
    """One entry of conversation history."""

    role: str                        # "user" | "assistant"
    content: str


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------


@dataclass
class Principal:
    """The caller.  ``departments`` drives security trimming at query time."""

    user_id: str
    display_name: str
    departments: list[str]           # ["*"] means unrestricted
    role: str = "employee"

    def can_see(self, department: str) -> bool:
        return "*" in self.departments or department in self.departments
