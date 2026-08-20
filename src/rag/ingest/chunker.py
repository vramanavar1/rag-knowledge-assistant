"""Chunking.

Two strategies live here, and the difference between them is most of the
measured improvement in ``eval/results/comparison.md``.

``chunk_baseline``  -- fixed-width character windows over the naive text dump,
                      no overlap, no structure, no metadata.  This is the
                      "before" system and it is meant to be genuinely naive,
                      not a strawman: it is exactly what a first-pass RAG
                      implementation looks like.

``chunk_document``  -- section-aware chunks that never split a table and that
                      carry a ``Title > Section`` breadcrumb plus the document's
                      department and effective date into the embedded text.

The breadcrumb is the important part.  A chunk is retrieved alone, without its
neighbours, so a chunk that reads "Tier 1 | New York, San Francisco, Boston,
London | $350" is unanswerable on its own -- nothing in it says "hotel", let
alone "nightly rate cap".  Prefixing the section heading makes the chunk match
"what is the hotel cap in London" both lexically (BM25) and semantically, and
it gives the model the context it needs to answer without guessing.
"""

from __future__ import annotations

import re

from rag.ingest.parsers import ParsedDoc
from rag.models import Chunk, DocumentMeta, make_chunk_id

TARGET_CHARS = 900
MAX_CHARS = 1400
OVERLAP_CHARS = 150
MAX_TABLE_CHARS = 3000

_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


def estimate_tokens(text: str) -> int:
    """Rough token count.

    Deliberately approximate: tiktoken is not a dependency, and every real
    token number reported by the app comes from the Azure OpenAI ``usage``
    field rather than from this estimate.  It is used only for budgeting
    context before a call is made.
    """
    return max(1, len(text) // 4)


def build_embed_text(meta: DocumentMeta, section_path: str, body: str,
                     content_type: str) -> str:
    """Prefix a chunk with the context needed to interpret it standalone.

    Note what is deliberately *absent*: whether the document is superseded.
    That status depends on which other documents exist, so baking it into the
    embedded text would mean re-embedding an untouched 2026 rate card the day a
    2027 one is added.  Currency is carried as a metadata field instead, which
    the ranker and the prompt builder read, and which can be patched in place.
    """
    descriptor = f"{meta.department} {meta.doc_type.replace('_', ' ')}"
    if meta.effective_date:
        descriptor += f", effective {meta.effective_date}"
    if meta.version:
        descriptor += f", version {meta.version}"

    heading = f"{meta.title} > {section_path}" if section_path else meta.title
    lead = f"{heading}\n[{descriptor}]"
    if content_type == "table":
        lead += f"\nTable from section '{section_path or meta.title}':"
    return f"{lead}\n\n{body}"


def _split_long_text(text: str) -> list[str]:
    """Split on sentence boundaries into overlapping windows."""
    if len(text) <= MAX_CHARS:
        return [text]

    sentences = _SENTENCE_END.split(text)
    parts: list[str] = []
    buffer = ""

    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip() if buffer else sentence
        if len(candidate) > TARGET_CHARS and buffer:
            parts.append(buffer.strip())
            tail = buffer[-OVERLAP_CHARS:]
            # Resume from a word boundary so the overlap reads cleanly.
            if " " in tail:
                tail = tail[tail.index(" ") + 1:]
            buffer = f"{tail} {sentence}".strip()
        else:
            buffer = candidate

    if buffer.strip():
        parts.append(buffer.strip())
    return parts or [text]


def _split_table(markdown: str) -> list[str]:
    """Split an oversized Markdown table by rows, repeating the header."""
    if len(markdown) <= MAX_TABLE_CHARS:
        return [markdown]

    lines = markdown.splitlines()
    if len(lines) < 3 or not lines[1].strip().startswith("|"):
        return [markdown]

    header, separator, rows = lines[0], lines[1], lines[2:]
    parts: list[str] = []
    buffer: list[str] = []
    budget = MAX_TABLE_CHARS - len(header) - len(separator)

    for row in rows:
        if buffer and sum(len(r) for r in buffer) + len(row) > budget:
            parts.append("\n".join([header, separator, *buffer]))
            buffer = []
        buffer.append(row)
    if buffer:
        parts.append("\n".join([header, separator, *buffer]))
    return parts


def chunk_document(parsed: ParsedDoc, meta: DocumentMeta) -> list[Chunk]:
    """Section-aware chunking (the ``improved`` profile)."""
    chunks: list[Chunk] = []
    section_path = ""
    section_page: int | None = None
    buffer: list[str] = []
    buffer_page: int | None = None

    def emit(body: str, content_type: str, page: int | None) -> None:
        body = body.strip()
        if not body:
            return
        ordinal = len(chunks)
        chunk_id = make_chunk_id(meta.doc_id, section_path or "front-matter", ordinal)
        embed_text = build_embed_text(meta, section_path, body, content_type)
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=meta.doc_id,
                ordinal=ordinal,
                section_path=section_path or "Front matter",
                content_type=content_type,
                text=body,
                embed_text=embed_text,
                page=page,
                token_estimate=estimate_tokens(embed_text),
                title=meta.title,
                department=meta.department,
                doc_type=meta.doc_type,
                version=meta.version,
                effective_date=meta.effective_date,
                is_current=meta.is_current,
                source_path=meta.path,
            )
        )

    def flush_buffer() -> None:
        nonlocal buffer, buffer_page
        if not buffer:
            return
        joined = "\n".join(buffer).strip()
        for part in _split_long_text(joined):
            emit(part, "text", buffer_page)
        buffer = []
        buffer_page = None

    for block in parsed.blocks:
        if block.kind in ("heading", "title"):
            flush_buffer()
            if block.kind == "heading":
                section_path = block.text
                section_page = block.page
            continue

        if block.kind == "table":
            flush_buffer()
            for part in _split_table(block.text):
                emit(part, "table", block.page or section_page)
            continue

        if buffer_page is None:
            buffer_page = block.page or section_page
        buffer.append(block.text)

        if sum(len(b) for b in buffer) >= MAX_CHARS:
            flush_buffer()

    flush_buffer()
    return chunks


def chunk_baseline(parsed: ParsedDoc, meta: DocumentMeta,
                   chunk_chars: int = 512) -> list[Chunk]:
    """Fixed-width chunking over the naive text dump (the ``baseline`` profile).

    No overlap, no section awareness, no breadcrumb -- and critically, it runs
    over ``naive_text``, so PDF tables arrive column-shuffled and DOCX tables
    arrive detached from their headings.
    """
    text = re.sub(r"\n{3,}", "\n\n", parsed.naive_text).strip()
    chunks: list[Chunk] = []

    for ordinal, start in enumerate(range(0, len(text), chunk_chars)):
        body = text[start:start + chunk_chars].strip()
        if not body:
            continue
        chunks.append(
            Chunk(
                chunk_id=make_chunk_id(meta.doc_id, "baseline", ordinal),
                doc_id=meta.doc_id,
                ordinal=ordinal,
                section_path=f"chars {start}-{start + len(body)}",
                content_type="text",
                text=body,
                embed_text=body,          # no breadcrumb: this is the point
                page=None,
                token_estimate=estimate_tokens(body),
                title=meta.title,
                department=meta.department,
                doc_type=meta.doc_type,
                version=meta.version,
                effective_date=meta.effective_date,
                is_current=meta.is_current,
                source_path=meta.path,
            )
        )
    return chunks
