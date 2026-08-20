"""Backend selection.

``RETRIEVER_BACKEND=local`` (default) or ``azure``.  The Azure adapter is
imported lazily so that the local path has no import-time coupling to it.
"""

from __future__ import annotations

from rag.config import Settings
from rag.observability.tracing import get_logger
from rag.store.base import SearchBackend
from rag.store.local import LocalHybridStore

log = get_logger(__name__)


def get_backend(settings: Settings) -> SearchBackend:
    if settings.retriever_backend == "azure":
        if not settings.has_azure_search:
            log.warning(
                "RETRIEVER_BACKEND=azure but AZURE_SEARCH_ENDPOINT/_API_KEY are "
                "not set; falling back to the local backend",
            )
        else:
            from rag.store.azure_search import AzureAISearchStore

            store = AzureAISearchStore(settings)
            log.info("search backend active", backend=store.name,
                     index=settings.search_index)
            return store

    store = LocalHybridStore(settings.index_path(), profile=settings.profile)
    store.load()
    log.info("search backend active", backend=store.name,
             path=str(settings.index_path()))
    return store
