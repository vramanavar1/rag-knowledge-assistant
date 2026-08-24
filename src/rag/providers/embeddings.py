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
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from rag.config import Settings
from rag.models import content_fingerprint
from rag.observability.tracing import get_logger, record_usage
from rag.providers.http import make_client, post_with_retry

log = get_logger(__name__)

_WORD_RE = re.compile(r"[a-z0-9$%.]+")
AZURE_BATCH_SIZE = 16


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


# --------------------------------------------------------------------------
# Local fallback
# --------------------------------------------------------------------------


class LocalEmbedder:
    """Signed feature hashing over words, word bigrams and character 4-grams."""

    name = "local-hashing"

    def __init__(self, dimensions: int = 768) -> None:
        self.dimensions = dimensions

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

    def embed(self, texts: list[str]) -> list[list[float]]:
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


# --------------------------------------------------------------------------
# Azure OpenAI
# --------------------------------------------------------------------------


class AzureOpenAIEmbedder:
    name = "azure-openai"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.deployment = settings.aoai_embedding_deployment
        self.dimensions = settings.aoai_embedding_dimensions
        self.name = f"azure-openai:{self.deployment}"
        self._client = make_client(settings, timeout_s=60.0)

    @property
    def _url(self) -> str:
        return (
            f"{self._settings.aoai_endpoint}/openai/deployments/"
            f"{self.deployment}/embeddings"
            f"?api-version={self._settings.aoai_api_version}"
        )

    def _post(self, payload: dict) -> dict:
        headers = {
            "api-key": self._settings.aoai_api_key,
            "Content-Type": "application/json",
        }
        response = post_with_retry(
            self._client, self._url, payload, headers,
            what="Azure OpenAI embeddings",
        )
        return response.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), AZURE_BATCH_SIZE):
            batch = texts[start:start + AZURE_BATCH_SIZE]
            payload: dict = {"input": batch}
            # text-embedding-3-* support shortening; older models reject the field.
            if "text-embedding-3" in self.deployment:
                payload["dimensions"] = self.dimensions

            data = self._post(payload)
            ordered = sorted(data["data"], key=lambda d: d["index"])
            vectors.extend(item["embedding"] for item in ordered)
            record_usage(
                prompt_tokens=data.get("usage", {}).get("prompt_tokens", 0),
                embedding_calls=1,
            )
        return vectors


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

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float] | None] = [None] * len(texts)
        missing: list[tuple[int, str]] = []

        for i, text in enumerate(texts):
            cached = self._cache.get(self._key(text))
            if cached is not None:
                results[i] = cached
            else:
                missing.append((i, text))

        if missing:
            fresh = self._inner.embed([t for _, t in missing])
            for (i, text), vector in zip(missing, fresh):
                results[i] = vector
                self._cache[self._key(text)] = vector
            self._dirty = True

        record_usage(cache_hits=len(texts) - len(missing))
        return [r for r in results if r is not None]


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def _probe(embedder: AzureOpenAIEmbedder) -> int | None:
    """Return the model's true vector width, or None if it is unreachable."""
    try:
        vector = embedder.embed(["probe"])
        return len(vector[0]) if vector and vector[0] else None
    except Exception as exc:
        log.warning(
            "Azure OpenAI embedding deployment unavailable, using local embedder",
            deployment=embedder.deployment,
            error=str(exc)[:200],
        )
        return None


def get_embedding_provider(settings: Settings, *, cached: bool = True) -> EmbeddingProvider:
    """Pick the best available embedder and say so out loud."""
    provider: EmbeddingProvider = LocalEmbedder()

    credentials = bool(settings.aoai_endpoint and settings.aoai_api_key
                       and settings.aoai_embedding_deployment)

    if credentials and not settings.aoai_enabled:
        # Same gate as the chat provider: inherited credentials must not turn a
        # "local" run into a billed cloud one without an explicit opt-in.
        log.warning(
            "Azure OpenAI embedding credentials are present but "
            "AZURE_OPENAI_ENABLED is not set, so the local embedder is used",
            deployment=settings.aoai_embedding_deployment,
            hint="set AZURE_OPENAI_ENABLED=true to use them",
        )
    elif credentials:
        candidate = AzureOpenAIEmbedder(settings)
        actual = _probe(candidate)
        if actual:
            # The probe reports the model's true width; trust it over the config.
            if actual != candidate.dimensions:
                log.info(
                    "embedding dimension corrected from probe",
                    configured=candidate.dimensions,
                    actual=actual,
                )
                candidate.dimensions = actual
            provider = candidate
    else:
        log.info(
            "no embedding deployment configured, using local embedder",
            hint="set AZURE_OPENAI_EMBEDDING_DEPLOYMENT to enable Azure embeddings",
        )

    log.info("embedding provider active", provider=provider.name,
             dimensions=provider.dimensions)

    if cached:
        return CachedEmbedder(provider, settings.embedding_cache_path())
    return provider
