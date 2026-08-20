"""Azure AI Search backend, over the REST API.

Implements the same ``SearchBackend`` contract as the local store, so nothing
in the retrieval pipeline changes when you switch to it.  What does change:

* **Hybrid is native.**  One request carries both the BM25 query and the vector
  query, and the service fuses them with RRF server-side.
* **Reranking moves in-service.**  With a semantic configuration, the L2
  reranker scores the fused candidates.  When that score is present the
  pipeline skips its own LLM reranker -- one fewer model call per question, and
  a large latency saving (the LLM rerank stage measures ~2s locally).
* **The analyzer is `en.microsoft`**, which does the stemming that
  :mod:`rag.text` implements by hand for the local backend.
* **Filters are OData** and are evaluated inside the query, so security
  trimming is enforced by the service rather than by the caller.

Why REST rather than ``azure-search-documents``: the SDK adds a dependency
without adding capability here, and the REST shapes below are exactly what the
portal and the docs show, which makes the index definition easy to audit.
"""

from __future__ import annotations

from typing import Any

from rag.config import Settings
from rag.models import Chunk, Hit
from rag.observability.tracing import get_logger
from rag.providers.http import make_client
from rag.store.base import (
    MODE_HYBRID,
    MODE_KEYWORD,
    MODE_VECTOR,
    IndexStats,
    SearchFilters,
)

log = get_logger(__name__)

UPLOAD_BATCH = 100
VECTOR_PROFILE = "hnsw-profile"
VECTOR_ALGORITHM = "hnsw-config"
SEMANTIC_CONFIG = "default-semantic"

# Azure's semantic reranker scores 0-4; the rest of this codebase reasons in
# 0-10, including the abstention threshold, so scores are rescaled on the way in.
_SEMANTIC_SCALE = 2.5

_SELECT_FIELDS = (
    "chunk_id,doc_id,ordinal,title,section_path,content,content_type,department,"
    "doc_type,version,effective_date,is_current,page,source_path,token_estimate"
)


def _quote(value: str) -> str:
    return value.replace("'", "''")


class AzureAISearchStore:
    name = "azure-ai-search"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._index = settings.search_index
        self._api = settings.search_api_version
        self._client = make_client(settings, timeout_s=60.0)
        self._dimensions = settings.aoai_embedding_dimensions
        self._embedding_provider = ""
        self._semantic_enabled = settings.search_semantic
        self._semantic_failed = False

    # ------------------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "api-key": self._settings.search_api_key,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.search_endpoint}{path}?api-version={self._api}"

    def _request(self, method: str, path: str,
                 payload: dict[str, Any] | None = None) -> Any:
        response = self._client.request(
            method, self._url(path), json=payload, headers=self._headers
        )
        if response.status_code in (200, 201, 204):
            return response.json() if response.content else None
        raise RuntimeError(
            f"Azure AI Search {method} {path} -> {response.status_code}: "
            f"{response.text[:400]}"
        )

    # ------------------------------------------------------------------
    # Index definition
    # ------------------------------------------------------------------

    def _index_definition(self, dimensions: int) -> dict[str, Any]:
        return {
            "name": self._index,
            "fields": [
                {"name": "chunk_id", "type": "Edm.String", "key": True,
                 "filterable": True, "searchable": False},
                {"name": "doc_id", "type": "Edm.String",
                 "filterable": True, "facetable": True},
                {"name": "ordinal", "type": "Edm.Int32", "sortable": True},
                {"name": "title", "type": "Edm.String", "searchable": True,
                 "analyzer": "en.microsoft"},
                {"name": "section_path", "type": "Edm.String", "searchable": True,
                 "filterable": True, "analyzer": "en.microsoft"},
                # `content` is what users see; `embed_text` carries the
                # breadcrumb and is what BM25 scores, matching the local backend.
                {"name": "content", "type": "Edm.String", "searchable": True,
                 "analyzer": "en.microsoft"},
                {"name": "embed_text", "type": "Edm.String", "searchable": True,
                 "analyzer": "en.microsoft"},
                {"name": "content_type", "type": "Edm.String",
                 "filterable": True, "facetable": True},
                {"name": "department", "type": "Edm.String",
                 "filterable": True, "facetable": True},
                {"name": "doc_type", "type": "Edm.String",
                 "filterable": True, "facetable": True},
                {"name": "version", "type": "Edm.String", "filterable": True},
                {"name": "effective_date", "type": "Edm.String",
                 "filterable": True, "sortable": True},
                {"name": "is_current", "type": "Edm.Boolean", "filterable": True},
                {"name": "page", "type": "Edm.Int32", "filterable": True},
                {"name": "source_path", "type": "Edm.String", "filterable": True},
                {"name": "token_estimate", "type": "Edm.Int32"},
                {
                    "name": "content_vector",
                    "type": "Collection(Edm.Single)",
                    "searchable": True,
                    "retrievable": False,
                    "dimensions": dimensions,
                    "vectorSearchProfile": VECTOR_PROFILE,
                },
            ],
            "vectorSearch": {
                "algorithms": [
                    {
                        "name": VECTOR_ALGORITHM,
                        "kind": "hnsw",
                        "hnswParameters": {
                            "m": 4,
                            "efConstruction": 400,
                            "efSearch": 500,
                            "metric": "cosine",
                        },
                    }
                ],
                "profiles": [
                    {"name": VECTOR_PROFILE, "algorithm": VECTOR_ALGORITHM}
                ],
            },
            "semantic": {
                "configurations": [
                    {
                        "name": SEMANTIC_CONFIG,
                        "prioritizedFields": {
                            "titleField": {"fieldName": "title"},
                            "prioritizedContentFields": [{"fieldName": "content"}],
                            "prioritizedKeywordsFields": [
                                {"fieldName": "section_path"}
                            ],
                        },
                    }
                ]
            },
        }

    def ensure_index(self, dimensions: int) -> None:
        self._dimensions = dimensions
        definition = self._index_definition(dimensions)
        # PUT is create-or-update and is idempotent, which is what we want on
        # every ingest run.
        self._request("PUT", f"/indexes/{self._index}", definition)
        log.info("azure ai search index ensured", index=self._index,
                 dimensions=dimensions, semantic=self._semantic_enabled)

    def set_embedding_provider(self, name: str) -> None:
        self._embedding_provider = name

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    @staticmethod
    def _to_document(chunk: Chunk, vector: list[float]) -> dict[str, Any]:
        return {
            "@search.action": "mergeOrUpload",
            "chunk_id": chunk.chunk_id,
            "doc_id": chunk.doc_id,
            "ordinal": chunk.ordinal,
            "title": chunk.title,
            "section_path": chunk.section_path,
            "content": chunk.text,
            "embed_text": chunk.embed_text,
            "content_type": chunk.content_type,
            "department": chunk.department,
            "doc_type": chunk.doc_type,
            "version": chunk.version,
            "effective_date": chunk.effective_date,
            "is_current": chunk.is_current,
            "page": chunk.page,
            "source_path": chunk.source_path,
            "token_estimate": chunk.token_estimate,
            "content_vector": vector,
        }

    def _index_batch(self, actions: list[dict[str, Any]]) -> None:
        for start in range(0, len(actions), UPLOAD_BATCH):
            batch = actions[start:start + UPLOAD_BATCH]
            result = self._request(
                "POST", f"/indexes/{self._index}/docs/index", {"value": batch}
            )
            failures = [
                r for r in (result or {}).get("value", []) if not r.get("status")
            ]
            if failures:
                log.error("azure ai search rejected documents",
                          count=len(failures),
                          first=failures[0].get("errorMessage", "")[:200])

    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if not chunks:
            return 0
        actions = [self._to_document(c, v) for c, v in zip(chunks, vectors)]
        self._index_batch(actions)
        return len(actions)

    def _chunk_ids_for_doc(self, doc_id: str) -> list[str]:
        ids: list[str] = []
        skip = 0
        while True:
            result = self._request(
                "POST",
                f"/indexes/{self._index}/docs/search",
                {
                    "search": "*",
                    "filter": f"doc_id eq '{_quote(doc_id)}'",
                    "select": "chunk_id",
                    "top": 1000,
                    "skip": skip,
                },
            )
            page = [row["chunk_id"] for row in (result or {}).get("value", [])]
            ids.extend(page)
            if len(page) < 1000:
                return ids
            skip += 1000

    def delete_by_doc(self, doc_id: str) -> int:
        ids = self._chunk_ids_for_doc(doc_id)
        if not ids:
            return 0
        self._index_batch(
            [{"@search.action": "delete", "chunk_id": cid} for cid in ids]
        )
        return len(ids)

    def patch_document_fields(self, doc_id: str, fields: dict[str, Any]) -> int:
        """Merge metadata onto a document's chunks without re-embedding them.

        ``merge`` leaves ``content_vector`` untouched, which is the whole point:
        marking a 2026 rate card superseded because a 2027 one arrived must not
        cost a single embedding call.
        """
        ids = self._chunk_ids_for_doc(doc_id)
        if not ids:
            return 0
        self._index_batch(
            [{"@search.action": "merge", "chunk_id": cid, **fields} for cid in ids]
        )
        return len(ids)

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    @staticmethod
    def _build_filter(filters: SearchFilters) -> str | None:
        clauses: list[str] = []
        if filters.departments is not None:
            values = ",".join(filters.departments)
            clauses.append(f"search.in(department, '{_quote(values)}', ',')")
        if filters.doc_ids is not None:
            values = ",".join(filters.doc_ids)
            clauses.append(f"search.in(doc_id, '{_quote(values)}', ',')")
        if filters.exclude_doc_ids:
            values = ",".join(filters.exclude_doc_ids)
            clauses.append(f"not search.in(doc_id, '{_quote(values)}', ',')")
        if filters.current_only:
            clauses.append("is_current eq true")
        if filters.content_types is not None:
            values = ",".join(filters.content_types)
            clauses.append(f"search.in(content_type, '{_quote(values)}', ',')")
        return " and ".join(clauses) if clauses else None

    @staticmethod
    def _to_hit(row: dict[str, Any]) -> Hit:
        chunk = Chunk(
            chunk_id=row["chunk_id"],
            doc_id=row.get("doc_id", ""),
            ordinal=row.get("ordinal") or 0,
            section_path=row.get("section_path") or "",
            content_type=row.get("content_type") or "text",
            text=row.get("content") or "",
            embed_text=row.get("content") or "",
            page=row.get("page"),
            token_estimate=row.get("token_estimate") or 0,
            title=row.get("title") or "",
            department=row.get("department") or "",
            doc_type=row.get("doc_type") or "",
            version=row.get("version"),
            effective_date=row.get("effective_date"),
            is_current=bool(row.get("is_current", True)),
            source_path=row.get("source_path") or "",
        )
        score = float(row.get("@search.score") or 0.0)
        reranker = row.get("@search.rerankerScore")
        return Hit(
            chunk=chunk,
            score=score,
            rrf_score=score,
            rerank_score=(float(reranker) * _SEMANTIC_SCALE
                          if reranker is not None else None),
        )

    def search(
        self,
        query: str,
        vector: list[float] | None,
        filters: SearchFilters,
        top_k: int,
        mode: str = MODE_HYBRID,
    ) -> list[Hit]:
        payload: dict[str, Any] = {
            "top": top_k,
            "select": _SELECT_FIELDS,
        }

        if odata := self._build_filter(filters):
            payload["filter"] = odata

        if mode in (MODE_HYBRID, MODE_KEYWORD):
            payload["search"] = query
            payload["searchFields"] = "embed_text,content,title,section_path"
        else:
            payload["search"] = "*"

        if mode in (MODE_HYBRID, MODE_VECTOR) and vector:
            payload["vectorQueries"] = [
                {
                    "kind": "vector",
                    "vector": vector,
                    "fields": "content_vector",
                    "k": top_k,
                }
            ]

        use_semantic = (
            self._semantic_enabled
            and not self._semantic_failed
            and mode in (MODE_HYBRID, MODE_KEYWORD)
        )
        if use_semantic:
            payload["queryType"] = "semantic"
            payload["semanticConfiguration"] = SEMANTIC_CONFIG

        try:
            result = self._request(
                "POST", f"/indexes/{self._index}/docs/search", payload
            )
        except RuntimeError as exc:
            if use_semantic:
                # Semantic ranking needs Basic tier or above. Degrade once,
                # loudly, and keep serving on plain hybrid.
                log.warning(
                    "semantic ranking unavailable, continuing without it "
                    "(requires Basic SKU or higher)",
                    error=str(exc)[:200],
                )
                self._semantic_failed = True
                payload.pop("queryType", None)
                payload.pop("semanticConfiguration", None)
                result = self._request(
                    "POST", f"/indexes/{self._index}/docs/search", payload
                )
            else:
                raise

        return [self._to_hit(row) for row in (result or {}).get("value", [])]

    # ------------------------------------------------------------------

    def _facets(self, fields: list[str]) -> dict[str, dict[str, int]]:
        """Facet counts for the whole index, in one request."""
        result = self._request(
            "POST",
            f"/indexes/{self._index}/docs/search",
            {
                "search": "*",
                "facets": [f"{f},count:1000" for f in fields],
                "top": 0,
            },
        )
        raw = (result or {}).get("@search.facets", {})
        return {
            field: {row["value"]: row.get("count", 0) for row in raw.get(field, [])}
            for field in fields
        }

    def document_ids(self) -> list[str]:
        return sorted(self._facets(["doc_id"])["doc_id"])

    def stats(self) -> IndexStats:
        try:
            counted = self._request(
                "GET", f"/indexes/{self._index}/docs/$count"
            )
            chunks = int(counted) if counted is not None else 0
        except (RuntimeError, ValueError):
            chunks = 0

        # doc_id and content_type in one facet request. Reporting table_chunks
        # here keeps the ingest summary identical across backends -- without it
        # the Azure path reports "0 tables" for an index that has plenty.
        documents, table_chunks = 0, 0
        try:
            facets = self._facets(["doc_id", "content_type"])
            documents = len(facets["doc_id"])
            table_chunks = facets["content_type"].get("table", 0)
        except RuntimeError:
            pass

        return IndexStats(
            backend=self.name,
            chunks=chunks,
            documents=documents,
            dimensions=self._dimensions,
            embedding_provider=self._embedding_provider,
            profile=self._settings.profile,
            extra={
                "index": self._index,
                "semantic": self._semantic_enabled and not self._semantic_failed,
                "table_chunks": table_chunks,
            },
        )

    def save(self) -> None:
        """No-op: the service is the store."""
