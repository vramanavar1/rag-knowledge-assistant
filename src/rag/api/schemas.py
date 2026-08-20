"""Request/response models for the HTTP API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from rag.models import Answer


class TurnModel(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[TurnModel] = Field(default_factory=list, max_length=20)
    include_trace: bool = True
    include_hits: bool = True
    use_cache: bool | None = None


class CitationModel(BaseModel):
    marker: str
    doc_id: str
    chunk_id: str
    title: str
    section_path: str
    page: int | None = None
    source_path: str = ""
    quote: str = ""


class HitModel(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    section_path: str
    department: str
    content_type: str
    is_current: bool
    page: int | None = None
    effective_date: str | None = None
    score: float
    rerank_score: float | None = None
    vector_score: float | None = None
    keyword_score: float | None = None
    rrf_score: float | None = None
    recency_boost: float = 0.0
    matched_subquery: str | None = None
    text: str


class ChatResponse(BaseModel):
    answer: str
    status: Literal[
        "answered", "insufficient_evidence", "needs_clarification", "denied"
    ]
    confidence: float
    citations: list[CitationModel] = Field(default_factory=list)
    clarification_options: list[str] = Field(default_factory=list)
    standalone_query: str = ""
    subqueries: list[str] = Field(default_factory=list)
    hits: list[HitModel] = Field(default_factory=list)
    trace: dict[str, Any] | None = None
    correlation_id: str = ""

    @classmethod
    def from_answer(
        cls,
        answer: Answer,
        correlation_id: str,
        *,
        include_trace: bool = True,
        include_hits: bool = True,
    ) -> "ChatResponse":
        return cls(
            answer=answer.text,
            status=answer.status,  # type: ignore[arg-type]
            confidence=answer.confidence,
            citations=[CitationModel(**c.__dict__) for c in answer.citations],
            clarification_options=answer.clarification_options,
            standalone_query=answer.standalone_query,
            subqueries=answer.subqueries,
            hits=[
                HitModel(
                    chunk_id=h.chunk.chunk_id,
                    doc_id=h.chunk.doc_id,
                    title=h.chunk.title,
                    section_path=h.chunk.section_path,
                    department=h.chunk.department,
                    content_type=h.chunk.content_type,
                    is_current=h.chunk.is_current,
                    page=h.chunk.page,
                    effective_date=h.chunk.effective_date,
                    score=round(h.score, 4),
                    rerank_score=h.rerank_score,
                    vector_score=(round(h.vector_score, 4)
                                  if h.vector_score is not None else None),
                    keyword_score=(round(h.keyword_score, 4)
                                   if h.keyword_score is not None else None),
                    rrf_score=(round(h.rrf_score, 5)
                               if h.rrf_score is not None else None),
                    recency_boost=h.recency_boost,
                    matched_subquery=h.matched_subquery,
                    text=h.chunk.text[:1500],
                )
                for h in (answer.hits if include_hits else [])
            ],
            trace=answer.trace if include_trace else None,
            correlation_id=correlation_id,
        )


class DocumentModel(BaseModel):
    doc_id: str
    title: str
    department: str
    doc_type: str
    version: str | None = None
    effective_date: str | None = None
    is_current: bool = True
    superseded_by: str | None = None
    chunks: int = 0


class IngestRequest(BaseModel):
    force: bool = False


class IngestResponse(BaseModel):
    new: list[str]
    modified: list[str]
    deleted: list[str]
    unchanged: list[str]
    superseded_now: list[str]
    reinstated: list[str]
    failed: dict[str, str]
    chunks_written: int
    chunks_purged: int
    total_chunks: int
    embedding_calls: int
    cache_hits: int


class ErrorResponse(BaseModel):
    error: str
    detail: str = ""
    correlation_id: str = ""
