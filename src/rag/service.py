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
from typing import Any

from rag.config import Settings, get_settings
from rag.generate.answer import AnswerGenerator
from rag.models import Answer, Principal, Turn
from rag.observability.tracing import (
    finish_trace,
    get_logger,
    start_trace,
)
from rag.providers.embeddings import get_embedding_provider
from rag.providers.llm import get_chat_provider
from rag.retrieve.pipeline import Retriever
from rag.store.factory import get_backend

log = get_logger(__name__)

_WHITESPACE = re.compile(r"\s+")


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
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.embedder = get_embedding_provider(self.settings)
        self.backend = get_backend(self.settings)
        self.llm = get_chat_provider(self.settings)
        self.llm.probe()

        self.retriever = Retriever(self.settings, self.backend, self.embedder, self.llm)
        self.generator = AnswerGenerator(self.settings, self.llm)
        self.cache = AnswerCache()

        stats = self.backend.stats()
        log.info(
            "assistant ready",
            profile=self.settings.profile,
            backend=stats.backend,
            chunks=stats.chunks,
            documents=stats.documents,
            embeddings=self.embedder.name,
            llm=self.llm.name,
        )

    # ------------------------------------------------------------------

    def ask(
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
                return Answer(
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

        outcome = self.retriever.retrieve(question, history, principal)
        answer = self.generator.generate(question, outcome, history, principal)

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

        # Only cache outcomes that are stable. A clarification depends on what
        # the user says next, and an abstention may be fixed by re-ingesting.
        if allow_cache and answer.status == "answered":
            self.cache.put(cache_key, answer)

        return answer

    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        stats = self.backend.stats()
        ready = stats.chunks > 0
        return {
            "status": "ready" if ready else "degraded",
            "profile": self.settings.profile,
            "index": {
                "backend": stats.backend,
                "documents": stats.documents,
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
            },
            "cache": {"hits": self.cache.hits, "misses": self.cache.misses},
            "detail": None if ready else "index is empty - run scripts/ingest.py",
        }
