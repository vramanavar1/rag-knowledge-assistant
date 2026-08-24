"""Ingest the knowledge base.

    python scripts/ingest.py                       # incremental, improved profile
    python scripts/ingest.py --profile baseline    # build the naive "before" index
    python scripts/ingest.py --force               # re-ingest everything
    python scripts/ingest.py --list                # show what is indexed

Re-running with no source changes performs zero parsing and zero embedding
calls; the summary line reports that explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rag.config import get_settings                                   # noqa: E402
from rag.ingest.manifest import Manifest                              # noqa: E402
from rag.ingest.sync import sync                                      # noqa: E402
from rag.observability.tracing import (                               # noqa: E402
    configure_logging,
    finish_trace,
    set_correlation_id,
    start_trace,
)
from rag.providers.embeddings import get_embedding_provider           # noqa: E402
from rag.store.factory import get_backend                             # noqa: E402


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Ingest the knowledge base.")
    parser.add_argument("--source", type=Path, default=None,
                        help="source directory (default: RAG_SOURCE_DIR)")
    parser.add_argument("--profile", choices=["improved", "baseline"], default=None,
                        help="which pipeline profile to build an index for")
    parser.add_argument("--backend", choices=["local", "azure"], default=None)
    parser.add_argument("--force", action="store_true",
                        help="re-ingest every document, ignoring content hashes")
    parser.add_argument("--list", action="store_true",
                        help="print the indexed documents and exit")
    parser.add_argument("--log-format", choices=["json", "text"], default="text")
    args = parser.parse_args()

    if args.profile:
        os.environ["RAG_PROFILE"] = args.profile
    if args.backend:
        os.environ["RETRIEVER_BACKEND"] = args.backend

    settings = get_settings(refresh=True)
    configure_logging(settings.log_level, args.log_format)
    set_correlation_id(None)
    start_trace()

    if args.list:
        manifest = Manifest(settings.manifest_path()).load()
        if not manifest.entries:
            print(f"nothing indexed for profile '{settings.profile}'")
            return 0
        print(f"{'document':34} {'dept':8} {'type':10} {'effective':11} "
              f"{'ver':5} {'cur':4} chunks")
        for entry in sorted(manifest.entries.values(), key=lambda e: e.doc_id):
            meta = entry.document_meta()
            print(
                f"{entry.doc_id:34} {meta.department:8} {meta.doc_type:10} "
                f"{(meta.effective_date or '-'):11} {(meta.version or '-'):5} "
                f"{('yes' if meta.is_current else 'NO'):4} {len(entry.chunk_ids)}"
            )
        return 0

    embedder = await get_embedding_provider(settings)
    backend = await get_backend(settings)

    report = await sync(
        settings,
        backend,
        embedder,
        source_dir=args.source or settings.source_dir,
        force=args.force,
    )

    print()
    print(report.render())
    trace = finish_trace()
    timings = "  ".join(f"{s['name']}={s['ms']}ms" for s in trace["stages"])
    print(f"  stages: {timings}")
    print(f"  total: {trace['total_ms']}ms")

    closer = getattr(backend, "aclose", None)
    if closer is not None:
        await closer()

    return 1 if report.failed else 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
