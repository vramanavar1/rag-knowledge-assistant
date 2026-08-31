"""Embedding providers.

Two implementations behind one protocol:

``AzureOpenAIEmbedder``  calls the Azure OpenAI embeddings REST endpoint.
``LocalEmbedder``        a deterministic signed-hashing bag-of-features vector,
                         used when no embedding deployment is reachable.

The local embedder is a genuine fallback, not a pretend one, and the README is
explicit about what it can and cannot do: it captures lexical and morphological
overlap (word unigrams, bigrams and character 4-grams) but it has no notion of
synonymy, so "time off" will not match "PTO" the way a real embedding does.
That is precisely why the hybrid retriever pairs it with BM25 and why the
startup log states which provider is live -- a demo must never quietly answer
from a weaker stack than the operator believes is running.

Vectors are cached by content fingerprint, so re-ingesting an unchanged corpus
costs zero embedding calls.

Batching and concurrency
------------------------
Azure caps how many inputs one embeddings call may carry, so a large ingest is
inevitably many calls.  Those calls are independent, so they go out concurrently
rather than one after another: at the measured 11.5 chunks per document, a
5M-document corpus is ~3.6M batches, and issuing them serially would mean
~3.6M round trips laid end to end.

``AZURE_EMBED_CONCURRENCY`` bounds that fan-out.  It is the back-pressure valve
against Azure OpenAI's tokens-per-minute quota -- unbounded ``gather`` over a
few million batches does not go faster, it just converts the whole job into
429s.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from rag.config import Settings
from rag.models import content_fingerprint
from rag.observability.tracing import get_logger, record_usage
from rag.providers.http import aclose as http_aclose
from rag.providers.http import make_client, post_with_retry

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9$%.]+")
AZURE_BATCH_SIZE = 16


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dimensions: int
    # Empty when the configured provider is live. Otherwise it names *why* this
    # is the fallback, in the operator's terms ("missing AZURE_OPENAI_API_KEY"),
    # so a startup contract failure can quote the cause rather than leaving it
    # in a log line the reader has to go and find.
    fallback_reason: str
    # Ordered record of how this provider was chosen. Surfaced on /health, so it
    # must never carry a secret -- see `_redact`.
    decision_trace: list[dict[str, Any]]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        ...


def _redact(value: str) -> str:
    """Render a secret as evidence of itself, never as itself."""
    if not value:
        return "<EMPTY>"
    if len(value) < 16:
        return f"<set len={len(value)}>"
    return f"<set len={len(value)} sha256:{content_fingerprint(value)[:8]}>"


# --------------------------------------------------------------------------
# Local fallback
# --------------------------------------------------------------------------


class LocalEmbedder:
    """Signed feature hashing over words, word bigrams and character 4-grams."""

    name = "local-hashing"

    def __init__(self, dimensions: int = 768, fallback_reason: str = "") -> None:
        self.dimensions = dimensions
        self.fallback_reason = fallback_reason
        self.decision_trace: list[dict[str, Any]] = []

    @staticmethod
    def _hash(token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        sign = 1.0 if value & 1 else -1.0
        return value >> 1, sign

    def _features(self, text: str) -> dict[str, float]:
        words = _WORD_RE.findall(text.lower())
        features: dict[str, float] = {}

        for word in words:
            features[f"w:{word}"] = features.get(f"w:{word}", 0.0) + 1.0
        for a, b in zip(words, words[1:]):
            key = f"b:{a}_{b}"
            features[key] = features.get(key, 0.0) + 0.5

        joined = " ".join(words)
        for i in range(len(joined) - 3):
            key = f"c:{joined[i:i + 4]}"
            features[key] = features.get(key, 0.0) + 0.3

        return features

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token, weight in self._features(text).items():
                index, sign = self._hash(token)
                # Sublinear term frequency, as in classic TF-IDF weighting.
                vector[index % self.dimensions] += sign * (1.0 + math.log(weight + 1.0))
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Hashing is CPU work with no network in it, so it goes to a worker
        # thread. Left inline it would run ON the event loop and stall every
        # other in-flight request -- `async def` alone does not make anything
        # non-blocking, and this is the fallback path every local run uses.
        if not texts:
            return []
        return await asyncio.to_thread(self._embed_sync, texts)


# --------------------------------------------------------------------------
# Azure OpenAI
# --------------------------------------------------------------------------


class AzureOpenAIEmbedder:
    name = "azure-openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.deployment = settings.aoai_embedding_deployment
        self.dimensions = settings.aoai_embedding_dimensions
        self._explicit_dimensions = settings.aoai_embedding_dimensions_explicit
        self.name = f"azure-openai:{self.deployment}"
        self.fallback_reason = ""       # this *is* the configured provider
        self.decision_trace: list[dict[str, Any]] = []
        self._client = make_client(settings, timeout_s=60.0)
        # Created lazily: a Semaphore binds to the running loop, and providers
        # are constructed before one exists (CLI, scripts, FastAPI import time).
        self._gate: asyncio.Semaphore | None = None

    def _semaphore(self) -> asyncio.Semaphore:
        if self._gate is None:
            self._gate = asyncio.Semaphore(self._settings.embed_concurrency)
        return self._gate

    @property
    def _url(self) -> str:
        return (
            f"{self._settings.aoai_endpoint}/openai/deployments/"
            f"{self.deployment}/embeddings"
            f"?api-version={self._settings.aoai_api_version}"
        )

    # Same URL, exposed for diagnostics. Safe to log and to return from /health:
    # the api-key travels in a header, so nothing secret appears here.
    @property
    def probe_url(self) -> str:
        return self._url

    async def _post(self, payload: dict) -> dict:
        headers = {
            "api-key": self._settings.aoai_api_key,
            "Content-Type": "application/json",
        }
        response = await post_with_retry(
            self._client, self._url, payload, headers,
            what="Azure OpenAI embeddings",
        )
        return response.json()

    async def probe_native_width(self) -> int:
        """The model's own vector width, measured without asking for one.

        Deliberately omits `dimensions`. Measuring through `embed()` -- which
        sends the configured width -- meant the probe received back exactly what
        it had asked for, so it could never contradict a misconfigured value. It
        was measuring its own input.
        """
        async with self._semaphore():
            data = await self._post({"input": ["probe"]})
        vectors = data.get("data") or []
        return len(vectors[0]["embedding"]) if vectors else 0

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict = {"input": batch}
        # Send the width only when one was explicitly configured, and let Azure
        # judge whether the model accepts it.
        #
        # This used to test `"text-embedding-3" in self.deployment` -- inferring
        # a *model* capability from a *deployment label* the operator chose. That
        # is wrong in both directions: a deployment named `vsquare-embeddings`
        # running text-embedding-3-small never received the field, while one
        # named `text-embedding-3-small` running ada-002 received it and 400'd.
        if self._explicit_dimensions:
            payload["dimensions"] = self.dimensions

        async with self._semaphore():
            data = await self._post(payload)

        ordered = sorted(data["data"], key=lambda d: d["index"])
        record_usage(
            prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
            embedding_calls=1,
        )
        return [item["embedding"] for item in ordered]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        batches = [
            texts[start:start + AZURE_BATCH_SIZE]
            for start in range(0, len(texts), AZURE_BATCH_SIZE)
        ]
        # gather preserves argument order, so the returned vectors still line up
        # with `texts` however the responses happen to arrive.
        results = await asyncio.gather(*(self._embed_batch(b) for b in batches))

        vectors: list[list[float]] = []
        for batch_vectors in results:
            vectors.extend(batch_vectors)
        return vectors

    async def aclose(self) -> None:
        await http_aclose(self._client)


# --------------------------------------------------------------------------
# Caching wrapper
# --------------------------------------------------------------------------


class CachedEmbedder:
    """Persistent content-addressed cache in front of any provider.

    The cache key includes the provider name and dimension, so switching from
    the local embedder to Azure (or changing dimensions) never returns a vector
    produced by a different model.
    """

    def __init__(self, inner: EmbeddingProvider, cache_path: Path) -> None:
        self._inner = inner
        self._path = cache_path
        self.name = inner.name
        self.dimensions = inner.dimensions
        # Forwarded, not recomputed: the wrapper must be indistinguishable from
        # what it wraps to anything inspecting the provider -- the startup
        # contract check sees only this object.
        self.fallback_reason = getattr(inner, "fallback_reason", "")
        self.decision_trace = getattr(inner, "decision_trace", [])
        self._cache: dict[str, list[float]] = {}
        self._dirty = False
        self._load()

    def _key(self, text: str) -> str:
        return f"{self.name}|{self.dimensions}|{content_fingerprint(text)}"

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._cache = json.loads(self._path.read_text(encoding="utf-8"))
            log.debug("embedding cache loaded", entries=len(self._cache))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("embedding cache unreadable, starting empty", error=str(exc))
            self._cache = {}

    def save(self) -> None:
        if not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache), encoding="utf-8")
        self._dirty = False
        log.debug("embedding cache saved", entries=len(self._cache))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str]] = []

        # Cache lookups are dict reads; they stay inline rather than going to a
        # thread, where the hand-off would cost more than the work.
        for i, text in enumerate(texts):
            cached = self._cache.get(self._key(text))
            if cached is not None:
                results[i] = cached
            else:
                missing.append((i, text))

        if missing:
            fresh = await self._inner.embed([t for _, t in missing])
            for (i, text), vector in zip(missing, fresh):
                results[i] = vector
                self._cache[self._key(text)] = vector
            self._dirty = True

        record_usage(cache_hits=len(texts) - len(missing))
        return [r for r in results if r is not None]

    async def aclose(self) -> None:
        inner_close = getattr(self._inner, "aclose", None)
        if inner_close is not None:
            await inner_close()


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


async def _probe(embedder: AzureOpenAIEmbedder) -> tuple[int | None, str]:
    """Return the model's true vector width, and why it failed if it did.

    The reason travels back with the result rather than only reaching the log:
    the startup contract check quotes it, and an operator reading
    "embedder fell back: probe failed: 404 DeploymentNotFound" needs no second
    source.
    """
    try:
        width = await embedder.probe_native_width()
        return (width or None), ""
    except Exception as exc:
        # Not truncated to a couple of hundred characters any more: Azure's
        # body carries the `code` ("DeploymentNotFound", "invalid_api_key") that
        # *is* the diagnosis, and cutting it short has cost real debugging time.
        detail = f"{type(exc).__name__}: {exc}"[:800]
        log.error(
            "Azure OpenAI embedding deployment unavailable, using local embedder",
            deployment=embedder.deployment,
            # The URL carries the deployment name and api-version -- the two
            # things most often wrong -- and never the key, which is a header.
            url=embedder.probe_url,
            error=detail,
        )
        return None, f"probe failed for deployment '{embedder.deployment}': {detail}"


async def get_embedding_provider(
    settings: Settings, *, cached: bool = True
) -> EmbeddingProvider:
    """Pick the best available embedder and say so out loud.

    Async because choosing correctly requires probing the deployment, and a
    constructor cannot await.
    """
    provider: EmbeddingProvider = LocalEmbedder()
    reason = ""

    # An ordered record of how this decision was reached, logged step by step and
    # carried on the provider for /health. The branch structure below is short,
    # but reconstructing which arm ran from a 500 three layers away is not -- so
    # it states each step as it takes it.
    trace: list[dict[str, Any]] = []

    def step(name: str, result: str, **fields: Any) -> None:
        entry = {"step": len(trace) + 1, "name": name, "result": result, **fields}
        trace.append(entry)
        log.info("embedding decision", **entry)

    credentials = bool(settings.aoai_endpoint and settings.aoai_api_key
                       and settings.aoai_embedding_deployment)

    step(
        "credentials",
        "complete" if credentials else "incomplete",
        endpoint=settings.aoai_endpoint or "<unset>",
        api_key=_redact(settings.aoai_api_key),
        deployment=settings.aoai_embedding_deployment or "<unset>",
    )
    step(
        "enabled",
        "proceed" if settings.aoai_enabled else "blocked",
        azure_openai_enabled=settings.aoai_enabled,
    )

    if credentials and not settings.aoai_enabled:
        # Same gate as the chat provider: inherited credentials must not turn a
        # "local" run into a billed cloud one without an explicit opt-in.
        reason = "AZURE_OPENAI_ENABLED is not true (credentials are present)"
        log.warning(
            "Azure OpenAI embedding credentials are present but "
            "AZURE_OPENAI_ENABLED is not set, so the local embedder is used",
            deployment=settings.aoai_embedding_deployment,
            hint="set AZURE_OPENAI_ENABLED=true to use them",
        )
    elif credentials:
        candidate = AzureOpenAIEmbedder(settings)
        actual, probe_error = await _probe(candidate)
        if actual:
            step("probe", "ok", url=candidate.probe_url, native_width=actual)
            if actual != candidate.dimensions:
                if candidate._explicit_dimensions:
                    # An explicit width is a deliberate truncation request, so it
                    # stands -- but say both numbers, because a width that does
                    # not match the index is the failure this whole path exists
                    # to make visible.
                    step("width", "configured overrides native",
                         configured=candidate.dimensions, native=actual)
                else:
                    step("width", "adopted from model",
                         was=candidate.dimensions, now=actual)
                    candidate.dimensions = actual
            provider = candidate
        else:
            reason = probe_error or "embedding probe returned no vector"
            step("probe", "FAILED", url=candidate.probe_url, error=reason)
    else:
        # Name the settings that are actually absent rather than guessing at one.
        # The expensive case is *partial* configuration -- endpoint and deployment
        # set but no key, say -- where a fixed "no embedding deployment
        # configured" message points at the one setting that is already correct.
        # That fallback is silent and its symptom is remote: the app embeds at
        # 768 while the index expects the model's width, and every query then
        # fails deep inside the search backend on a vector-dimension mismatch.
        missing = [name for name, value in (
            ("AZURE_OPENAI_ENDPOINT", settings.aoai_endpoint),
            ("AZURE_OPENAI_API_KEY", settings.aoai_api_key),
            ("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", settings.aoai_embedding_deployment),
        ) if not value]
        reason = f"missing {', '.join(missing)}"
        # ERROR, not INFO: with RETRIEVER_BACKEND=azure this is very often the
        # cause of a width mismatch that stops the app booting, and it needs to
        # be findable in the log without knowing what to grep for.
        log.error(
            "falling back to the local embedder",
            missing=",".join(missing),
            hint=f"set {' and '.join(missing)} to use Azure embeddings",
        )

    if reason:
        provider = LocalEmbedder(fallback_reason=reason)
        step("fallback", "local-hashing",
             width=provider.dimensions, reason=reason)

    step("result", provider.name, width=provider.dimensions)
    provider.decision_trace = trace

    log.info("embedding provider active", provider=provider.name,
             dimensions=provider.dimensions,
             fallback_reason=provider.fallback_reason)

    if cached:
        return CachedEmbedder(provider, settings.embedding_cache_path())
    return provider
