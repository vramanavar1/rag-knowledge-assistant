"""Incremental ingestion: classify, purge, re-ingest, reconcile.

This is the document lifecycle described in docs/ingestion-flow.md.  The order
of operations matters and is not obvious, so it is spelled out here:

1. Scan and classify against the manifest (new / modified / unchanged / deleted).
2. Parse only the new and modified documents.
3. Reconcile versions across the *whole* corpus -- fresh metadata for what was
   parsed, manifest metadata for everything else.  This happens BEFORE chunking
   so that new chunks are written with a correct ``is_current`` from the start.
4. Purge: deleted documents lose all their chunks; modified documents lose
   their previous chunks before the new ones are written.
5. Chunk, embed (cache-checked) and upsert the new and modified documents.
6. Patch the documents whose currency changed without their bytes changing --
   adding Pricing2027.pdf demotes Pricing2026.pdf, which was never re-parsed.
7. Persist manifest, embedding cache and index.

Step 6 is the one a naive implementation omits, and its absence is silent: the
index keeps serving a superseded document as current until someone happens to
re-ingest the file that was replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag.config import Settings
from rag.ingest.chunker import chunk_baseline, chunk_document
from rag.ingest.manifest import ChangeSet, Manifest
from rag.ingest.metadata import VersionHints, extract_metadata, reconcile_versions
from rag.ingest.parsers import ParsedDoc, parse_document
from rag.models import Chunk, DocumentMeta
from rag.observability.tracing import current_trace, get_logger, stage
from rag.providers.embeddings import EmbeddingProvider
from rag.store.base import SearchBackend

log = get_logger(__name__)


@dataclass
class SyncReport:
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    superseded_now: list[str] = field(default_factory=list)
    reinstated: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    chunks_written: int = 0
    chunks_purged: int = 0
    embedding_calls: int = 0
    cache_hits: int = 0
    total_chunks: int = 0
    table_chunks: int = 0
    embedding_provider: str = ""
    backend: str = ""
    profile: str = ""

    def render(self) -> str:
        lines = [
            f"profile={self.profile}  backend={self.backend}  "
            f"embeddings={self.embedding_provider}",
            f"  {len(self.new)} new, {len(self.modified)} modified, "
            f"{len(self.deleted)} deleted, {len(self.unchanged)} unchanged",
            f"  chunks: +{self.chunks_written} written, -{self.chunks_purged} purged, "
            f"{self.total_chunks} total ({self.table_chunks} tables)",
            f"  embeddings: {self.embedding_calls} API batches, "
            f"{self.cache_hits} cache hits",
        ]
        for doc_id in self.superseded_now:
            lines.append(f"  superseded: {doc_id}")
        for doc_id in self.reinstated:
            lines.append(f"  reinstated as current: {doc_id}")
        for doc_id, error in self.failed.items():
            lines.append(f"  FAILED {doc_id}: {error}")
        return "\n".join(lines)


def _parse_one(
    doc_id: str,
    path: Path,
    source_dir: Path,
    content_hash: str,
) -> tuple[ParsedDoc, DocumentMeta, VersionHints]:
    parsed = parse_document(path)
    full_text = "\n".join(block.text for block in parsed.blocks) or parsed.naive_text
    meta, hints = extract_metadata(
        path=path,
        source_root=source_dir,
        title=parsed.title,
        header_line=parsed.header_line,
        full_text=full_text,
        content_hash=content_hash,
        page_count=parsed.page_count,
    )
    return parsed, meta, hints


def sync(
    settings: Settings,
    backend: SearchBackend,
    embedder: EmbeddingProvider,
    *,
    source_dir: Path | None = None,
    manifest: Manifest | None = None,
    force: bool = False,
) -> SyncReport:
    source_dir = source_dir or settings.source_dir
    manifest = manifest or Manifest(settings.manifest_path()).load()

    report = SyncReport(
        profile=settings.profile,
        backend=getattr(backend, "name", "unknown"),
        embedding_provider=getattr(embedder, "name", "unknown"),
    )

    if not source_dir.exists():
        raise FileNotFoundError(f"source directory not found: {source_dir}")

    # ---- 1. classify --------------------------------------------------
    with stage("scan"):
        changes: ChangeSet = manifest.scan(source_dir)
        if force:
            changes.modified += changes.unchanged
            changes.unchanged = []
        report.new, report.modified = list(changes.new), list(changes.modified)
        report.deleted, report.unchanged = list(changes.deleted), list(changes.unchanged)
    log.info("scan complete", summary=changes.summary(), source=str(source_dir))

    # ---- 2. parse the touched documents -------------------------------
    parsed_docs: dict[str, ParsedDoc] = {}
    metas: dict[str, DocumentMeta] = {}
    hints: dict[str, VersionHints] = {}

    with stage("parse") as st:
        for doc_id in changes.touched:
            try:
                parsed, meta, hint = _parse_one(
                    doc_id, changes.paths[doc_id], source_dir, changes.hashes[doc_id]
                )
            except Exception as exc:
                # One malformed document must not stall the corpus.  In
                # production this is the poison-queue path.
                log.exception("failed to parse document", doc_id=doc_id)
                report.failed[doc_id] = str(exc)[:200]
                continue
            parsed_docs[doc_id] = parsed
            metas[doc_id] = meta
            hints[doc_id] = hint
        st["documents"] = len(parsed_docs)

    failed = set(report.failed)
    changes.new = [d for d in changes.new if d not in failed]
    changes.modified = [d for d in changes.modified if d not in failed]

    # Everything still indexed keeps its stored metadata for reconciliation.
    for doc_id in changes.unchanged:
        if entry := manifest.entries.get(doc_id):
            metas[doc_id] = entry.document_meta()
            hints[doc_id] = entry.version_hints()

    # ---- 3. reconcile versions across the whole corpus ----------------
    with stage("reconcile") as st:
        was_current = {doc_id: meta.is_current for doc_id, meta in metas.items()}
        changed_ids = reconcile_versions(metas, hints)
        st["changed"] = len(changed_ids)
        for doc_id in changed_ids:
            if metas[doc_id].is_current and not was_current.get(doc_id, True):
                report.reinstated.append(doc_id)
            elif not metas[doc_id].is_current:
                report.superseded_now.append(doc_id)

    # ---- 4. purge -----------------------------------------------------
    with stage("purge") as st:
        purged = 0
        for doc_id in changes.deleted:
            purged += backend.delete_by_doc(doc_id)
            manifest.forget(doc_id)
            log.info("document deleted from index", doc_id=doc_id)
        for doc_id in changes.modified:
            purged += backend.delete_by_doc(doc_id)
        report.chunks_purged = purged
        st["chunks"] = purged

    # ---- 5. chunk, embed, upsert --------------------------------------
    backend.ensure_index(embedder.dimensions)
    if hasattr(backend, "set_embedding_provider"):
        backend.set_embedding_provider(embedder.name)

    with stage("index") as st:
        written = 0
        for doc_id in changes.touched:
            parsed = parsed_docs.get(doc_id)
            if parsed is None:
                continue
            meta = metas[doc_id]

            chunks: list[Chunk] = (
                chunk_baseline(parsed, meta, settings.baseline_chunk_chars)
                if settings.is_baseline
                else chunk_document(parsed, meta)
            )
            if not chunks:
                log.warning("document produced no chunks", doc_id=doc_id)

            vectors = embedder.embed([c.embed_text for c in chunks]) if chunks else []
            backend.upsert(chunks, vectors)
            written += len(chunks)

            manifest.record(
                doc_id=doc_id,
                path_rel=doc_id,
                content_hash=changes.hashes[doc_id],
                size=changes.sizes[doc_id],
                chunk_ids=[c.chunk_id for c in chunks],
                meta=meta,
                hints=hints[doc_id],
            )
            log.info(
                "document indexed",
                doc_id=doc_id,
                chunks=len(chunks),
                tables=sum(1 for c in chunks if c.content_type == "table"),
                department=meta.department,
                is_current=meta.is_current,
            )
        report.chunks_written = written
        st["chunks"] = written

    # ---- 6. patch documents whose currency changed without a re-parse --
    with stage("patch") as st:
        patched = 0
        for doc_id in changed_ids:
            if doc_id in changes.touched or doc_id not in manifest.entries:
                continue
            meta = metas[doc_id]
            patched += backend.patch_document_fields(
                doc_id, {"is_current": meta.is_current}
            )
            manifest.update_meta(doc_id, meta)
            log.info(
                "document currency patched without re-embedding",
                doc_id=doc_id,
                is_current=meta.is_current,
                superseded_by=meta.superseded_by,
            )
        st["chunks"] = patched

    # Keep the manifest's copy of metadata in step with the reconciled truth.
    for doc_id in changes.touched:
        if doc_id in metas:
            manifest.update_meta(doc_id, metas[doc_id])

    # ---- 7. persist ---------------------------------------------------
    with stage("persist"):
        manifest.save()
        if hasattr(embedder, "save"):
            embedder.save()
        backend.save()

    stats = backend.stats()
    report.total_chunks = stats.chunks
    report.table_chunks = stats.extra.get("table_chunks", 0)

    trace: dict[str, Any] = current_trace() or {}
    report.embedding_calls = trace.get("embedding_calls", 0)
    report.cache_hits = trace.get("cache_hits", 0)
    return report
