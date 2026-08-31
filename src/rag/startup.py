"""Bootstrap contract checks.

A misconfiguration that cannot possibly serve a correct answer should stop the
process at boot, not surface as a 500 on someone's first question. Everything
needed to detect it is already known at startup; until now it was only logged at
``INFO``, where it scrolls past.

**Scope: ``RETRIEVER_BACKEND=azure`` only.** The local paths degrade on purpose
-- that is what makes a zero-credential demo possible -- and nothing here
changes them. Asking for Azure explicitly is the signal that degradation is
*not* acceptable.

**What is actually checked is width agreement, not "did something fall back".**
Azure AI Search does not generate embeddings; it stores float arrays and
compares them, with ``content_vector``'s width fixed when the index is created.
So the local hashing embedder works perfectly well against Azure AI Search *when
the same embedder wrote the index* -- ingest puts 768-wide vectors in a 768-wide
index and queries arrive 768-wide. Lower quality, but correct.

The failure is mixing: a 1536-wide index built by ``text-embedding-3-small``,
queried at 768 by the fallback. Azure rejects on width; and had the widths
coincidentally matched, it would instead have returned confident nonsense,
because vectors from two different models share no space. Width is therefore the
contract, and a fallback whose width agrees is a working configuration rather
than a broken one.
"""

from __future__ import annotations

from dataclasses import dataclass

from rag.config import Settings
from rag.observability.tracing import get_logger
from rag.providers.embeddings import EmbeddingProvider
from rag.store.base import SearchBackend

log = get_logger(__name__)

AZURE_BACKEND = "azure"
# Matched by name rather than by class so this module keeps the lazy-import
# property `store/factory.py` is built around -- the local path must not import
# the Azure adapter. Compared against what we *expect*, not against the local
# store's name, so any substitute store is caught, not just that one.
AZURE_STORE_NAME = "azure-ai-search"


class StartupContractError(RuntimeError):
    """Raised when the wiring cannot serve a correct answer.

    The message is deliberately self-contained. It becomes uvicorn's exit line,
    which is often the only thing that survives in a crash-looping container.
    """


@dataclass
class Violation:
    check: str
    expected: str
    actual: str
    reason: str
    fix: str


async def check_startup_contract(
    settings: Settings,
    embedder: EmbeddingProvider,
    backend: SearchBackend,
) -> list[Violation]:
    """Log what is wired up and return anything that cannot work.

    Returns rather than raises: the caller owns the policy, because "refuse to
    serve" and "exit the process" are different answers and only one of them
    leaves something behind to interrogate. Called once, from
    ``AssistantService.create``.
    """
    wants_azure = settings.retriever_backend == AZURE_BACKEND
    store_fell_back = wants_azure and backend.name != AZURE_STORE_NAME
    fallback_reason = getattr(embedder, "fallback_reason", "")

    # 0 means "no opinion" -- an empty or not-yet-created index. Never a mismatch.
    index_width = 0 if store_fell_back else await backend.vector_width()

    violations: list[Violation] = []

    if store_fell_back:
        missing = [
            name for name, value in (
                ("AZURE_SEARCH_ENDPOINT", settings.search_endpoint),
                ("AZURE_SEARCH_API_KEY", settings.search_api_key),
            ) if not value
        ]
        violations.append(Violation(
            check="store_backend_agreement",
            expected="azure-ai-search",
            actual=backend.name,
            reason=f"missing {', '.join(missing)}" if missing
                   else "the Azure store could not be constructed",
            fix="set the missing variable(s); a secretref that resolves to an "
                "empty string looks identical to a correct one in the portal",
        ))

    elif wants_azure and index_width and index_width != embedder.dimensions:
        violations.append(Violation(
            check="index_embedder_width_agreement",
            expected=f"{index_width} (index {settings.search_index}, "
                     f"field content_vector)",
            actual=f"{embedder.dimensions} (embedder {embedder.name})",
            reason=f"embedder fell back: {fallback_reason}" if fallback_reason
                   else "the embedder is configured to a width the index was "
                        "not built with",
            fix=f"restore an embedder producing {index_width}-wide vectors, or "
                f"re-ingest the corpus at {embedder.dimensions}",
        ))

    summary = {
        "retriever_backend": settings.retriever_backend,
        "store": backend.name,
        "store_fallback": store_fell_back,
        "index": settings.search_index if wants_azure else str(settings.index_path()),
        "index_vector_width": index_width,
        "embedder": embedder.name,
        "embedder_width": embedder.dimensions,
        "embedder_fallback": bool(fallback_reason),
        "embedder_fallback_reason": fallback_reason,
        "aoai_enabled": settings.aoai_enabled,
        "aoai_credentials_present": settings.azure_openai_credentials_present,
    }

    if not violations:
        log.info("startup contract ok", **summary)
        return []

    for v in violations:
        log.error(
            "startup contract violated",
            check=v.check, expected=v.expected, actual=v.actual,
            reason=v.reason, fix=v.fix, **summary,
        )

    return violations


def describe(violation: Violation) -> str:
    """One self-contained line. Becomes uvicorn's exit message under `crash`,
    which is often all that survives of a container that never started."""
    return (
        f"{violation.check}: expected {violation.expected}, "
        f"got {violation.actual}. {violation.reason}. Fix: {violation.fix}"
    )
