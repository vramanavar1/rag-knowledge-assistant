"""The ingestion manifest: what is currently indexed, and what it produced.

The manifest is the small piece of state that turns "re-run the ingester" into
a genuine incremental sync.  For every document it records the hash of the
bytes that were indexed and the exact set of ``chunk_id``s those bytes
produced.  That second field is what makes deletion correct: without it, a
document edited from nine sections down to seven leaves two orphan chunks in
the index that still match queries and still get cited, which surfaces to users
as a confident answer citing a paragraph that no longer exists.

In production this same state lives in a table (Blob metadata or Cosmos DB)
rather than a JSON file; the shape is identical.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag.ingest.metadata import VersionHints
from rag.ingest.parsers import SUPPORTED_EXTENSIONS
from rag.models import DocumentMeta
from rag.observability.tracing import get_logger

log = get_logger(__name__)


def file_hash(path: Path) -> str:
    """SHA-256 of the file's bytes.

    Content, not mtime: copying a file or re-saving it without edits changes
    the timestamp but not the meaning, and re-embedding it would be pure cost.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ManifestEntry:
    doc_id: str
    path: str
    content_hash: str
    size: int
    chunk_ids: list[str] = field(default_factory=list)
    ingested_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    hints: dict[str, Any] = field(default_factory=dict)

    def document_meta(self) -> DocumentMeta:
        return DocumentMeta.from_dict(self.meta)

    def version_hints(self) -> VersionHints:
        known = {f for f in VersionHints.__dataclass_fields__}
        return VersionHints(**{k: v for k, v in self.hints.items() if k in known})


@dataclass
class ChangeSet:
    new: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    # doc_id -> absolute path, for everything currently on disk
    paths: dict[str, Path] = field(default_factory=dict)
    hashes: dict[str, str] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)

    @property
    def touched(self) -> list[str]:
        return self.new + self.modified

    def summary(self) -> str:
        return (
            f"{len(self.new)} new, {len(self.modified)} modified, "
            f"{len(self.deleted)} deleted, {len(self.unchanged)} unchanged"
        )


class Manifest:
    def __init__(self, path: Path) -> None:
        self._path = path
        self.entries: dict[str, ManifestEntry] = {}

    # ------------------------------------------------------------------

    def load(self) -> "Manifest":
        if not self._path.exists():
            return self
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("manifest unreadable, treating corpus as new",
                        path=str(self._path), error=str(exc))
            return self
        for record in raw.get("documents", []):
            entry = ManifestEntry(**record)
            self.entries[entry.doc_id] = entry
        log.debug("manifest loaded", documents=len(self.entries))
        return self

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": utc_now(),
            "documents": [asdict(e) for e in self.entries.values()],
        }
        self._path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.debug("manifest saved", documents=len(self.entries))

    # ------------------------------------------------------------------

    def scan(self, source_dir: Path) -> ChangeSet:
        """Classify every document under ``source_dir`` against the manifest."""
        changes = ChangeSet()
        seen: set[str] = set()

        for path in sorted(source_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            # Skip Office lock files (~$Foo.docx) that appear when a doc is open.
            if path.name.startswith("~$"):
                continue

            doc_id = path.relative_to(source_dir).as_posix()
            seen.add(doc_id)
            digest = file_hash(path)

            changes.paths[doc_id] = path
            changes.hashes[doc_id] = digest
            changes.sizes[doc_id] = path.stat().st_size

            existing = self.entries.get(doc_id)
            if existing is None:
                changes.new.append(doc_id)
            elif existing.content_hash != digest:
                changes.modified.append(doc_id)
            else:
                changes.unchanged.append(doc_id)

        changes.deleted = sorted(set(self.entries) - seen)
        return changes

    # ------------------------------------------------------------------

    def record(
        self,
        doc_id: str,
        path_rel: str,
        content_hash: str,
        size: int,
        chunk_ids: list[str],
        meta: DocumentMeta,
        hints: VersionHints,
    ) -> None:
        self.entries[doc_id] = ManifestEntry(
            doc_id=doc_id,
            path=path_rel,
            content_hash=content_hash,
            size=size,
            chunk_ids=chunk_ids,
            ingested_at=utc_now(),
            meta=meta.to_dict(),
            hints=asdict(hints),
        )

    def forget(self, doc_id: str) -> ManifestEntry | None:
        return self.entries.pop(doc_id, None)

    def update_meta(self, doc_id: str, meta: DocumentMeta) -> None:
        if entry := self.entries.get(doc_id):
            entry.meta = meta.to_dict()

    def all_meta(self) -> dict[str, DocumentMeta]:
        return {doc_id: e.document_meta() for doc_id, e in self.entries.items()}

    def all_hints(self) -> dict[str, VersionHints]:
        return {doc_id: e.version_hints() for doc_id, e in self.entries.items()}
