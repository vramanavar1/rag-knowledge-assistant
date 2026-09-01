"""The assembled assistant.

One object wires the providers, the backend, the retriever and the generator
together, and it is the single entry point used by both the HTTP API and the
evaluation harness -- so what the evaluation measures is exactly what the API
serves.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any

from rag.config import Settings, get_settings, redacted_env
from rag.generate.answer import AnswerGenerator
from rag.models import Answer, Principal, Turn
from rag.observability.tracing import (
    finish_trace,
    get_logger,
    start_trace,
)
from rag.providers.embeddings import EmbeddingProvider, get_embedding_provider
from rag.providers.llm import ChatProvider, get_chat_provider
from rag.retrieve.pipeline import Retriever
from rag.startup import StartupContractError, check_startup_contract, describe
from rag.store.base import SearchBackend
from rag.store.factory import get_backend

log = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")

# Bounds for the text the trail carries. One line per question lands in Log
# Analytics, and the corpus should not be duplicated there chunk by chunk.
_TRAIL_TEXT_CHARS = 500
_TRAIL_SNIPPET_CHARS = 200


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "…"


def _trail_hits(hits: list[Any], *, with_text: bool) -> list[dict[str, Any]]:
    """One compact record per retrieved chunk.

    Every score is kept separate rather than fused, because the questions this
    exists to answer are "was the reranker applied at all?" and "which signal
    put this chunk on top?" -- neither is answerable from a single number.
    """
    records = []
    for h in hits:
        c = h.chunk
        record: dict[str, Any] = {
            "chunk_id": c.chunk_id,
            "doc_id": c.doc_id,
            "department": c.department,
            "section_path": c.section_path,
            # Currency travels with the scores: an answer can be perfectly
            # grounded and still wrong if its source has been superseded.
            "is_current": c.is_current,
            "version": c.version,
            "effective_date": c.effective_date,
            "vector_score": h.vector_score,
            "keyword_score": h.keyword_score,
            "rrf_score": h.rrf_score,
            # None here across every row is the answer to "did the reranker run?"
            # `rerank_score` is calibrated so it is comparable across rerankers;
            # `rerank_raw` is what the reranker itself returned, which is what
            # you check against Azure's 0-4 scale or the LLM's 0-10.
            "rerank_score": h.rerank_score,
            "rerank_raw": h.rerank_raw,
            "recency_boost": h.recency_boost,
            "score": round(h.score, 4),
            "matched_subquery": h.matched_subquery,
        }
        if with_text:
            record["snippet"] = _clip(c.text, _TRAIL_SNIPPET_CHARS)
        records.append(record)
    return records


class AnswerCache:
    """Small LRU over single-turn questions.

    Only single-turn questions are cached.  A follow-up's meaning depends on
    the conversation that preceded it, so caching "what about Standard?" by its
    own text would serve one user's answer to another user's question.  The
    embedding cache underneath handles the repeated-query cost case regardless
    of turn depth.
    """

    def __init__(self, capacity: int = 256) -> None:
        self._capacity = capacity
        self._entries: OrderedDict[str, Answer] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def key(question: str, principal: Principal | None, profile: str) -> str:
        normalised = _WHITESPACE.sub(" ", question.strip().lower()).rstrip("?.!")
        scope = ",".join(sorted(principal.departments)) if principal else "*"
        return f"{profile}|{scope}|{normalised}"

    def get(self, key: str) -> Answer | None:
        with self._lock:
            answer = self._entries.get(key)
            if answer is None:
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return answer

    def put(self, key: str, answer: Answer) -> None:
        with self._lock:
            self._entries[key] = answer
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class AssistantService:
    """Construct with ``await AssistantService.create(settings)``.

    Wiring the service requires I/O -- probing the chat deployment, probing the
    embedding model for its true vector width, loading the index -- and a
    ``__init__`` cannot ``await``.  So ``__init__`` only assigns what it is
    handed, and ``create()`` does the awaiting.  The alternative, blocking
    inside the constructor, would stall the event loop at startup and make the
    class unusable from anywhere already inside one.
    """

    def __init__(
        self,
        settings: Settings,
        embedder: EmbeddingProvider,
        backend: SearchBackend,
        llm: ChatProvider,
    ) -> None:
        self.settings = settings
        self.embedder = embedder
        self.backend = backend
        self.llm = llm

        # Populated by `create`. Non-empty means the wiring cannot serve a
        # correct answer: readiness fails and /chat refuses, quoting the reason.
        self.startup_errors: list[dict[str, Any]] = []

        self.retriever = Retriever(settings, backend, embedder, llm)
        self.generator = AnswerGenerator(settings, llm)
        self.cache = AnswerCache()
        self._health_cache: tuple[float, Any] | None = None

    @classmethod
    async def create(cls, settings: Settings | None = None) -> "AssistantService":
        settings = settings or get_settings()
        embedder = await get_embedding_provider(settings)
        backend = await get_backend(settings)
        llm = get_chat_provider(settings)
        await llm.probe()

        # A wiring that cannot serve a correct answer must not quietly 500 on the
        # first question. Under `unready` (the default) the process stays up but
        # fails readiness, so the reason is reachable at /health -- a crashed
        # container has no replica, and therefore no /health and no log stream,
        # which is exactly when the diagnosis is most wanted. Local runs are
        # unaffected either way.
        violations = await check_startup_contract(settings, embedder, backend)
        if violations and settings.startup_fail_mode == "crash":
            raise StartupContractError(describe(violations[0]))

        service = cls(settings, embedder, backend, llm)
        service.startup_errors = [asdict(v) for v in violations]

        stats = await backend.stats(full=True)   # once, at startup
        log.info(
            "assistant ready",
            profile=settings.profile,
            backend=stats.backend,
            chunks=stats.chunks,
            documents=stats.documents,
            embeddings=embedder.name,
            llm=llm.name,
        )
        return service

    async def aclose(self) -> None:
        """Release every connection pool the service owns."""
        for owner in (self.llm, self.embedder, self.backend):
            closer = getattr(owner, "aclose", None)
            if closer is not None:
                await closer()

    # ------------------------------------------------------------------

    async def ask(
        self,
        question: str,
        history: list[Turn] | None = None,
        principal: Principal | None = None,
        *,
        use_cache: bool | None = None,
    ) -> Answer:
        started = time.perf_counter()
        start_trace()

        history = history or []
        allow_cache = (
            (self.settings.enable_answer_cache if use_cache is None else use_cache)
            and not history
        )
        cache_key = AnswerCache.key(question, principal, self.settings.profile)

        if allow_cache:
            if cached := self.cache.get(cache_key):
                trace = dict(cached.trace)
                trace["cache"] = "hit"
                trace["total_ms"] = round((time.perf_counter() - started) * 1000, 1)
                log.info("answer served from cache", question=question)
                served = Answer(
                    text=cached.text,
                    status=cached.status,
                    citations=cached.citations,
                    confidence=cached.confidence,
                    hits=cached.hits,
                    standalone_query=cached.standalone_query,
                    subqueries=cached.subqueries,
                    clarification_options=cached.clarification_options,
                    trace=trace,
                )
                # A cached answer is still an answer someone received, so it
                # gets a trail too -- `cache: "hit"` distinguishes it. Omitting
                # it would leave gaps in the record for the most-asked questions.
                self._log_answer_trail(question, served)
                return served

        outcome = await self.retriever.retrieve(question, history, principal)
        answer = await self.generator.generate(question, outcome, history, principal)

        trace = finish_trace()
        answer.trace.update(
            {
                "cache": "miss" if allow_cache else "bypass",
                "stages": trace.get("stages", []),
                "tokens": trace.get("tokens", {}),
                "llm_calls": trace.get("llm_calls", 0),
                "embedding_calls": trace.get("embedding_calls", 0),
                "correlation_id": trace.get("correlation_id"),
                "total_ms": trace.get("total_ms"),
                "providers": {
                    "embeddings": self.embedder.name,
                    "llm": self.llm.name,
                    "backend": getattr(self.backend, "name", "unknown"),
                },
            }
        )

        self._log_answer_trail(question, answer)

        # Only cache outcomes that are stable. A clarification depends on what
        # the user says next, and an abstention may be fixed by re-ingesting.
        if allow_cache and answer.status == "answered":
            self.cache.put(cache_key, answer)

        return answer

    def _log_answer_trail(self, question: str, answer: Answer) -> None:
        """One structured line carrying the whole story of this request.

        Everything here was already computed and returned in the response body;
        it simply never reached stdout, which is the only route to Log Analytics.
        Without it a correlation id yields the `request` line and nothing about
        why the answer came out the way it did.
        """
        trace = answer.trace or {}
        grounded = trace.get("groundedness") or {}
        cited_docs = {c.doc_id for c in answer.citations if hasattr(c, "doc_id")}

        fields: dict[str, Any] = {
            "status": answer.status,
            "confidence": round(answer.confidence, 3),
            "cache": trace.get("cache"),
            "total_ms": trace.get("total_ms"),
            "hit_count": len(answer.hits),
            "rerank_method": trace.get("rerank_method"),
            "versioning": trace.get("versioning"),
            "groundedness": grounded.get("score"),
            "groundedness_method": grounded.get("method"),
            "citations_valid": grounded.get("citations_valid"),
            # The truthfulness signal groundedness alone cannot give: a faithful
            # answer drawn from a superseded document is still wrong.
            #
            # Scoped to *cited* documents on purpose. Retrieval routinely turns
            # up an older version and demotes it -- that is the version ranking
            # working, not a fault. And with no citations there is nothing the
            # answer was grounded in, so an abstention that happened to retrieve
            # a stale chunk must not raise this flag.
            "grounded_in_superseded": any(
                not h.chunk.is_current
                for h in answer.hits if h.chunk.doc_id in cited_docs
            ),
            "cited_docs": sorted(cited_docs),
            "stages": trace.get("stages"),
            "tokens": trace.get("tokens"),
            "llm_calls": trace.get("llm_calls"),
            "embedding_calls": trace.get("embedding_calls"),
            "providers": trace.get("providers"),
            "hits": _trail_hits(answer.hits,
                                with_text=self.settings.log_answer_trail),
        }

        if self.settings.log_answer_trail:
            fields["question"] = _clip(question, _TRAIL_TEXT_CHARS)
            fields["standalone_query"] = answer.standalone_query
            fields["subqueries"] = answer.subqueries
            fields["answer"] = _clip(answer.text, _TRAIL_TEXT_CHARS)

        log.info("answer trail", **fields)

    # ------------------------------------------------------------------

    # A readiness probe is polled every few seconds per replica, so its cost is
    # multiplied by replica count and never amortised. Even the cheap `$count`
    # is worth not issuing on every poll.
    _HEALTH_TTL_SECONDS = 15.0

    async def health(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._health_cache and now - self._health_cache[0] < self._HEALTH_TTL_SECONDS:
            stats = self._health_cache[1]
        else:
            stats = await self.backend.stats()      # cheap path: no aggregation
            self._health_cache = (now, stats)
        # A contract violation outranks an empty index: it is the reason the
        # instance must not take traffic, and it is what the operator needs to
        # read first.
        ready = stats.chunks > 0 and not self.startup_errors

        # Tri-state, read back off the decision trace rather than kept as new
        # state: True probed and answered, False probed and refused, None never
        # attempted. Collapsing the last two into `false` would report a local
        # run as broken.
        trace = getattr(self.embedder, "decision_trace", [])
        probe = next((s for s in trace if s.get("name") == "probe"), None)
        reachable = None if probe is None else probe.get("result") == "ok"

        report: dict[str, Any] = {
            "status": "ready" if ready else "degraded",
            "profile": self.settings.profile,
            "index": {
                "backend": stats.backend,
                # Omitted on the cheap path rather than reported as 0 or as a
                # capped count that stops being true past the facet limit.
                "documents": stats.documents if stats.documents_exact else None,
                "chunks": stats.chunks,
                "dimensions": stats.dimensions,
                "embedding_provider": stats.embedding_provider,
            },
            "providers": {
                "embeddings": self.embedder.name,
                "llm": self.llm.name,
                "llm_available": self.llm.available,
                # Distinguishes "no credentials" from "credentials present but
                # not switched on", which look identical from the outside.
                "azure_openai_enabled": self.settings.aoai_enabled,
                "azure_openai_credentials_present":
                    self.settings.azure_openai_credentials_present,
                # Did the embedding deployment actually answer at boot? Without
                # this, a refused endpoint under RETRIEVER_BACKEND=local shows up
                # as nothing but `embeddings: local-hashing` -- indistinguishable
                # from a deliberate offline run.
                "azure_openai_reachable": reachable,
                "embeddings_fallback_reason":
                    getattr(self.embedder, "fallback_reason", "") or None,
            },
            "cache": {"hits": self.cache.hits, "misses": self.cache.misses},
            "detail": (
                None if ready
                else self.startup_errors[0]["reason"] if self.startup_errors
                else "index is empty - run scripts/ingest.py"
            ),
        }

        # A refused endpoint ships its trace too, not just a contract violation.
        # The violation only fires under RETRIEVER_BACKEND=azure against an index
        # of a different width -- so on every other configuration the reason the
        # embedder fell back used to be reachable only by setting a flag and
        # redeploying, which is the one thing you cannot do to a live symptom.
        if self.startup_errors or reachable is False:
            if self.startup_errors:
                report["startup_errors"] = self.startup_errors
            # The trace carries no secret -- see `_redact` in
            # providers/embeddings.py.
            if trace:
                report["embedding_decision"] = trace

        # Opt-in troubleshooting block. Values are redacted by `redacted_env()`
        # itself, not here -- the flag decides whether the block exists, never
        # whether a secret is legible.
        if self.settings.show_env_values:
            report["env"] = redacted_env()
            report.setdefault("embedding_decision",
                              getattr(self.embedder, "decision_trace", []))

        return report
