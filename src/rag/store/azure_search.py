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

import re
import time
from typing import Any

from rag.config import Settings
from rag.models import Chunk, Hit
from rag.observability.tracing import get_logger
from rag.providers.http import aclose as http_aclose
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
# Named once because two things must agree on it: the field this store *creates*
# in `_index_definition`, and the field `vector_width` reads back to check what
# the index actually holds. A literal in both places is a silent trap.
VECTOR_FIELD = "content_vector"
VECTOR_PROFILE = "hnsw-profile"
VECTOR_ALGORITHM = "hnsw-config"
SEMANTIC_CONFIG = "default-semantic"

# Facets return at most this many buckets. Any count that reaches it is a
# lower bound, not a total.
FACET_LIMIT = 1000

# Azure's semantic reranker scores 0-4; the rest of this codebase reasons in
# 0-10, including the abstention threshold, so scores are rescaled on the way in.
_SEMANTIC_SCALE = 2.5

# Azure's own wording when the service cannot do semantic ranking at all. Every
# failed search arrives as the same RuntimeError, so without matching on the
# message there is no way to tell "this tier has no semantic ranker" from "this
# one query was malformed" -- and mistaking the second for the first disables a
# working feature. A vector-dimension mismatch was reported as a missing SKU for
# exactly this reason.
_SEMANTIC_UNAVAILABLE = re.compile(
    r"semantic (search|ranking) is not enabled"
    r"|semantic[^.]{0,40}not (supported|available)"
    r"|requires[^.]{0,40}(basic|standard) (tier|sku)",
    re.I,
)

# How long to stay on plain hybrid after a genuine semantic failure. Deliberately
# not permanent: a tier upgrade or a transient outage should heal on its own,
# where a process-lifetime flag needs a revision restart nobody thinks to do.
# The cost of being wrong is one failed request per interval.
SEMANTIC_RETRY_SECONDS = 900.0

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
        # The width the index *really* has, read back from its definition. None
        # until first asked; 0 once asked and the index does not exist.
        self._vector_width: int | None = None
        self._embedding_provider = ""
        self._semantic_enabled = settings.search_semantic
        # Monotonic deadline before semantic is retried; 0.0 means "active now".
        self._semantic_retry_at = 0.0

    # ------------------------------------------------------------------

    @property
    def _semantic_active(self) -> bool:
        """Configured on, and not inside a back-off window."""
        return self._semantic_enabled and time.monotonic() >= self._semantic_retry_at

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "api-key": self._settings.search_api_key,
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self._settings.search_endpoint}{path}?api-version={self._api}"

    async def _request(self, method: str, path: str,
                       payload: dict[str, Any] | None = None) -> Any:
        response = await self._client.request(
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
                    "name": VECTOR_FIELD,
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

    async def vector_width(self) -> int:
        """Width of `content_vector` as the live index declares it, or 0.

        Read from the index definition rather than from settings, because the
        question this answers is "what did whoever ingested actually use?" --
        which configuration cannot know.

        **This never raises.** It is called during startup, and an index it
        cannot read is a reason to skip the width check -- not a reason to take
        the container down. Raising here meant a 403 from a stale search key, or
        one transient network blip at boot, turned into a restart loop with no
        live replica to attach a log stream to. Whatever went wrong is logged and
        surfaces on the next real query, exactly as it did before this check
        existed.
        """
        if self._vector_width is not None:
            return self._vector_width

        self._vector_width = 0          # cached even on failure: retrying per
                                        # probe would hammer a broken service
        try:
            response = await self._client.request(
                "GET", self._url(f"/indexes/{self._index}"), headers=self._headers
            )
        except Exception as exc:                                # noqa: BLE001
            log.error(
                "could not reach Azure AI Search to read the index definition; "
                "the embedding width check is skipped",
                index=self._index, error=f"{type(exc).__name__}: {exc}"[:400],
            )
            return 0

        if response.status_code == 404:
            log.info("azure ai search index does not exist yet", index=self._index)
            return 0
        if response.status_code != 200:
            log.error(
                "could not read the index definition; the embedding width check "
                "is skipped",
                index=self._index, status=response.status_code,
                error=response.text[:400],
                hint="403 usually means AZURE_SEARCH_API_KEY is wrong or lacks "
                     "rights on this index",
            )
            return 0

        fields = response.json().get("fields", [])
        self._vector_width = next(
            (int(f.get("dimensions") or 0)
             for f in fields if f.get("name") == VECTOR_FIELD),
            0,
        )
        return self._vector_width

    async def ensure_index(self, dimensions: int) -> None:
        self._dimensions = dimensions
        # The definition is about to change, so anything read earlier is stale.
        self._vector_width = None
        definition = self._index_definition(dimensions)
        # PUT is create-or-update and is idempotent, which is what we want on
        # every ingest run.
        await self._request("PUT", f"/indexes/{self._index}", definition)
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

    async def _index_batch(self, actions: list[dict[str, Any]]) -> None:
        for start in range(0, len(actions), UPLOAD_BATCH):
            batch = actions[start:start + UPLOAD_BATCH]
            result = await self._request(
                "POST", f"/indexes/{self._index}/docs/index", {"value": batch}
            )
            failures = [
                r for r in (result or {}).get("value", []) if not r.get("status")
            ]
            if failures:
                log.error("azure ai search rejected documents",
                          count=len(failures),
                          first=failures[0].get("errorMessage", "")[:200])

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> int:
        if not chunks:
            return 0
        actions = [self._to_document(c, v) for c, v in zip(chunks, vectors)]
        await self._index_batch(actions)
        return len(actions)

    async def _chunk_ids_for_doc(self, doc_id: str) -> list[str]:
        ids: list[str] = []
        skip = 0
        while True:
            result = await self._request(
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

    async def delete_by_doc(self, doc_id: str) -> int:
        ids = await self._chunk_ids_for_doc(doc_id)
        if not ids:
            return 0
        await self._index_batch(
            [{"@search.action": "delete", "chunk_id": cid} for cid in ids]
        )
        return len(ids)

    async def patch_document_fields(self, doc_id: str, fields: dict[str, Any]) -> int:
        """Merge metadata onto a document's chunks without re-embedding them.

        ``merge`` leaves ``content_vector`` untouched, which is the whole point:
        marking a 2026 rate card superseded because a 2027 one arrived must not
        cost a single embedding call.
        """
        ids = await self._chunk_ids_for_doc(doc_id)
        if not ids:
            return 0
        await self._index_batch(
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

    async def search(
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
            self._semantic_active
            and mode in (MODE_HYBRID, MODE_KEYWORD)
        )
        if use_semantic:
            payload["queryType"] = "semantic"
            payload["semanticConfiguration"] = SEMANTIC_CONFIG

        try:
            result = await self._request(
                "POST", f"/indexes/{self._index}/docs/search", payload
            )
        except RuntimeError as exc:
            # Only Azure saying semantic is unavailable disables semantic.
            # Anything else -- a malformed query, a vector-width mismatch, a
            # throttle -- would fail the retry identically, so retrying costs a
            # second doomed request and the warning names the wrong cause.
            # Re-raising surfaces the real error on the first attempt.
            if not (use_semantic and _SEMANTIC_UNAVAILABLE.search(str(exc))):
                raise

            # Report what Azure said rather than asserting why. The previous
            # message claimed "requires Basic SKU or higher" without checking,
            # which sent a vector-dimension bug to the wrong department.
            log.warning(
                "semantic ranking unavailable, continuing on plain hybrid",
                error=str(exc)[:400],
                retry_in_s=SEMANTIC_RETRY_SECONDS,
            )
            self._semantic_retry_at = time.monotonic() + SEMANTIC_RETRY_SECONDS
            payload.pop("queryType", None)
            payload.pop("semanticConfiguration", None)
            result = await self._request(
                "POST", f"/indexes/{self._index}/docs/search", payload
            )

        return [self._to_hit(row) for row in (result or {}).get("value", [])]

    # ------------------------------------------------------------------

    async def _facets(self, fields: list[str]) -> dict[str, dict[str, int]]:
        """Facet counts for the whole index, in one request."""
        result = await self._request(
            "POST",
            f"/indexes/{self._index}/docs/search",
            {
                "search": "*",
                "facets": [f"{f},count:{FACET_LIMIT}" for f in fields],
                "top": 0,
            },
        )
        raw = (result or {}).get("@search.facets", {})
        return {
            field: {row["value"]: row.get("count", 0) for row in raw.get(field, [])}
            for field in fields
        }

    async def document_ids(self) -> list[str]:
        return sorted((await self._facets(["doc_id"]))["doc_id"])

    async def stats(self, *, full: bool = False) -> IndexStats:
        """Index statistics.

        ``full=False`` issues a single ``$count`` and nothing else. That matters
        because ``/health`` calls this, and a readiness probe polled every few
        seconds must not run an aggregation across tens of millions of documents.

        ``full=True`` adds the facet query that yields document and table counts,
        and is for ingest reporting and startup — once, not per probe.
        """
        # The index's real width, not the configured one. Reporting config here
        # meant /health would confirm whatever the operator had set even when the
        # index disagreed -- corroborating the wrong answer during exactly the
        # investigation /health exists to shorten. Memoised, so this costs one
        # request for the life of the process, not one per probe.
        width = await self.vector_width()

        try:
            counted = await self._request(
                "GET", f"/indexes/{self._index}/docs/$count"
            )
            chunks = int(counted) if counted is not None else 0
        except (RuntimeError, ValueError):
            chunks = 0

        if not full:
            return IndexStats(
                backend=self.name,
                chunks=chunks,
                documents=0,
                documents_exact=False,      # not computed, not zero
                dimensions=width,
                embedding_provider=self._embedding_provider,
                profile=self._settings.profile,
                extra={
                    "index": self._index,
                    "semantic": self._semantic_active,
                },
            )

        # doc_id and content_type in one facet request. Reporting table_chunks
        # here keeps the ingest summary identical across backends -- without it
        # the Azure path reports "0 tables" for an index that has plenty.
        documents, table_chunks, exact = 0, 0, True
        try:
            facets = await self._facets(["doc_id", "content_type"])
            documents = len(facets["doc_id"])
            table_chunks = facets["content_type"].get("table", 0)
            # A facet returns at most FACET_LIMIT buckets. Hitting the cap means
            # "at least this many", not "exactly this many" -- reporting the
            # capped number as a count is silently wrong past the limit.
            exact = documents < FACET_LIMIT
            if not exact:
                log.warning(
                    "document count is a lower bound: the facet limit was reached",
                    limit=FACET_LIMIT,
                    hint="enumerate doc_ids from the ingestion manifest instead",
                )
        except RuntimeError:
            exact = False

        return IndexStats(
            backend=self.name,
            chunks=chunks,
            documents=documents,
            documents_exact=exact,
            dimensions=width,
            embedding_provider=self._embedding_provider,
            profile=self._settings.profile,
            extra={
                "index": self._index,
                "semantic": self._semantic_active,
                "table_chunks": table_chunks,
            },
        )

    async def save(self) -> None:
        """No-op: the service is the store."""

    async def aclose(self) -> None:
        await http_aclose(self._client)
