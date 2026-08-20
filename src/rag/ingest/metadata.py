"""Document-level metadata extraction and version reconciliation.

Retrieval quality in this corpus depends on metadata as much as on embeddings.
Two fields do most of the work:

``department``  drives security trimming (assignment Step 5, Q4) and is taken
                from the folder the document lives in, which is the same thing
                a real deployment gets from the container / SharePoint library
                the file was ingested from.

``is_current``  drives version-aware ranking (Scenario 3).  It is deliberately
                *not* computed per document in isolation -- it is resolved
                across the whole corpus by :func:`reconcile_versions`, because
                whether Pricing2026 is current depends on whether a Pricing2027
                exists.  Dropping a new file into the source folder therefore
                demotes its predecessor without that predecessor changing.

Version links are established in three ways, in priority order:

1. An explicit forward link in the text  -- "supersedes Pricing2025.pdf".
2. An explicit backward link            -- "See Pricing2026.pdf for current rates".
3. A filename/title series convention   -- Pricing2025 / Pricing2026 / Pricing2027
                                           share a series and the later year wins.

(3) is what makes the system keep working when a new rate card is added with no
supersession sentence in it at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rag.models import DocumentMeta
from rag.observability.tracing import get_logger

log = get_logger(__name__)

# Folder name -> canonical department. The folder is authoritative.
DEPARTMENTS = ("Finance", "HR", "IT", "Legal", "Sales")

_DEPARTMENT_ALIASES = {
    "finance department": "Finance",
    "human resources": "HR",
    "people operations": "HR",
    "information technology": "IT",
    "legal department": "Legal",
    "sales operations": "Sales",
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

_FILE_REF = r"([A-Za-z0-9_\-]+\.(?:pdf|docx|xlsx))"

_SUPERSEDES_FORWARD = re.compile(
    rf"(?:supersedes?|replaces?)\s+(?:the\s+)?{_FILE_REF}", re.I
)
_SUPERSEDES_BACKWARD = re.compile(
    rf"see\s+{_FILE_REF}\s+for\s+(?:the\s+)?(?:current|latest|new)", re.I
)
_SUPERSEDES_RAW = re.compile(r"supersede[sd]?(?:\s+by)?\s*:\s*([^|\n]+)", re.I)

_VERSION = re.compile(r"(?:template\s+)?version\s*:?\s*v?(\d+(?:\.\d+)?)", re.I)
_EFFECTIVE = re.compile(
    r"(?:effective|plan year|last updated)\s*:?\s*([^|\n]+)", re.I
)
_YEAR_IN_NAME = re.compile(r"(19|20)\d{2}")


# --------------------------------------------------------------------------
# Scalar extraction
# --------------------------------------------------------------------------


def parse_date(text: str) -> tuple[str | None, str | None]:
    """Parse the date expressions this corpus actually uses.

    Returns ``(effective_from, effective_until)`` as ISO dates.
    Handles "February 1, 2026", "January 1, 2025 - December 31, 2025",
    "March 2026" and a bare "2026".
    """
    if not text:
        return None, None
    text = text.strip()

    # Split a range on en/em dash or the word "to" (but not inside "2-Year").
    parts = re.split(r"\s+[–—-]\s+|\s+to\s+", text, maxsplit=1)

    def one(fragment: str) -> str | None:
        fragment = fragment.strip().rstrip(".,;")
        m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})", fragment)
        if m and m.group(1).lower() in _MONTHS:
            return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
        m = re.search(r"([A-Za-z]+)\s+((?:19|20)\d{2})", fragment)
        if m and m.group(1).lower() in _MONTHS:
            return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}-01"
        m = re.search(r"\b((?:19|20)\d{2})\b", fragment)
        if m:
            return f"{m.group(1)}-01-01"
        return None

    start = one(parts[0])
    end = one(parts[1]) if len(parts) > 1 else None
    return start, end


def infer_department(path: Path, source_root: Path, header_line: str) -> str:
    """Folder first, header strapline as a fallback."""
    try:
        relative = path.relative_to(source_root)
        top = relative.parts[0] if len(relative.parts) > 1 else ""
    except ValueError:
        top = ""

    for dept in DEPARTMENTS:
        if top.lower() == dept.lower():
            return dept

    lowered = header_line.lower()
    for alias, dept in _DEPARTMENT_ALIASES.items():
        if alias in lowered:
            return dept
    return top or "General"


def infer_doc_type(title: str, path: Path) -> str:
    haystack = f"{title} {path.stem}".lower()
    if "rate card" in haystack or "pricing" in haystack:
        return "rate_card"
    if "schedule" in haystack or "discount" in haystack:
        return "schedule"
    if "agreement" in haystack or "template" in haystack or "nda" in haystack:
        return "template"
    if "guide" in haystack:
        return "guide"
    if "policy" in haystack:
        return "policy"
    return "document"


def series_key(path: Path, department: str, doc_type: str) -> tuple[str | None, int | None]:
    """Identify a versioned document series from its filename.

    ``Sales/Pricing2026.pdf`` -> ``("sales|rate_card|pricing", 2026)``.
    Returns ``(None, None)`` for documents with no year in the name.
    """
    stem = path.stem
    match = _YEAR_IN_NAME.search(stem)
    if not match:
        return None, None
    year = int(match.group(0))
    base = _YEAR_IN_NAME.sub("", stem).strip(" _-").lower()
    if not base:
        return None, None
    return f"{department.lower()}|{doc_type}|{base}", year


# --------------------------------------------------------------------------
# Document metadata
# --------------------------------------------------------------------------


@dataclass
class VersionHints:
    """Raw supersession signals found in a document, resolved later."""

    supersedes_file: str | None = None      # this doc replaces that file
    superseded_by_file: str | None = None   # that file replaces this doc
    supersedes_raw: str | None = None       # verbatim text, for display
    series: str | None = None
    year: int | None = None


def extract_metadata(
    path: Path,
    source_root: Path,
    title: str,
    header_line: str,
    full_text: str,
    content_hash: str,
    page_count: int | None,
) -> tuple[DocumentMeta, VersionHints]:
    doc_id = path.relative_to(source_root).as_posix()
    department = infer_department(path, source_root, header_line)
    doc_type = infer_doc_type(title, path)

    version = None
    if m := _VERSION.search(header_line) or _VERSION.search(full_text[:1500]):
        version = m.group(1)

    effective_from = effective_until = None
    if m := _EFFECTIVE.search(header_line) or _EFFECTIVE.search(full_text[:1500]):
        effective_from, effective_until = parse_date(m.group(1))

    hints = VersionHints()
    if m := _SUPERSEDES_FORWARD.search(full_text):
        hints.supersedes_file = m.group(1)
    if m := _SUPERSEDES_BACKWARD.search(full_text):
        hints.superseded_by_file = m.group(1)
    if m := _SUPERSEDES_RAW.search(header_line):
        hints.supersedes_raw = m.group(1).strip()
    hints.series, hints.year = series_key(path, department, doc_type)

    meta = DocumentMeta(
        doc_id=doc_id,
        path=doc_id,
        title=title,
        department=department,
        doc_type=doc_type,
        content_hash=content_hash,
        source_ext=path.suffix.lower(),
        version=version,
        effective_date=effective_from,
        effective_until=effective_until,
        supersedes_raw=hints.supersedes_raw,
        page_count=page_count,
    )
    return meta, hints


# --------------------------------------------------------------------------
# Corpus-wide reconciliation
# --------------------------------------------------------------------------


def reconcile_versions(
    metas: dict[str, DocumentMeta],
    hints: dict[str, VersionHints],
) -> list[str]:
    """Resolve supersession across the whole corpus.

    Mutates ``metas`` in place and returns the ids of documents whose
    ``is_current`` / ``superseded_by`` changed, so the caller knows which
    already-indexed chunks need their denormalised copy patched.

    This must run after every ingest, including runs where nothing was parsed:
    adding Pricing2027.pdf changes Pricing2026.pdf without touching its bytes.
    """
    before = {doc_id: (m.is_current, m.superseded_by) for doc_id, m in metas.items()}

    for meta in metas.values():
        meta.is_current = True
        meta.superseded_by = None
        meta.supersedes_doc_id = None

    # Filename -> doc_id, so "supersedes Pricing2025.pdf" can be resolved.
    by_filename: dict[str, list[str]] = {}
    for doc_id in metas:
        by_filename.setdefault(Path(doc_id).name.lower(), []).append(doc_id)

    def resolve(filename: str | None, prefer_department: str) -> str | None:
        if not filename:
            return None
        candidates = by_filename.get(filename.lower(), [])
        if not candidates:
            return None
        for candidate in candidates:
            if metas[candidate].department == prefer_department:
                return candidate
        return candidates[0]

    # (1) + (2): explicit links, in both directions.
    edges: dict[str, str] = {}  # older doc_id -> newer doc_id
    for doc_id, hint in hints.items():
        department = metas[doc_id].department
        if older := resolve(hint.supersedes_file, department):
            if older != doc_id:
                edges[older] = doc_id
        if newer := resolve(hint.superseded_by_file, department):
            if newer != doc_id:
                edges[doc_id] = newer

    # (3) filename series convention, only where no explicit link exists.
    series_groups: dict[str, list[tuple[int, str]]] = {}
    for doc_id, hint in hints.items():
        if hint.series and hint.year:
            series_groups.setdefault(hint.series, []).append((hint.year, doc_id))

    for members in series_groups.values():
        if len(members) < 2:
            continue
        members.sort()
        for (_year, older), (_next_year, newer) in zip(members, members[1:]):
            edges.setdefault(older, newer)

    for older, newer in edges.items():
        metas[older].is_current = False
        metas[older].superseded_by = newer
        metas[newer].supersedes_doc_id = older

    changed = [
        doc_id
        for doc_id, meta in metas.items()
        if before.get(doc_id) != (meta.is_current, meta.superseded_by)
    ]
    if changed:
        log.info(
            "version reconciliation updated documents",
            changed=len(changed),
            superseded=[d for d in changed if not metas[d].is_current],
        )
    return changed
