"""Prove the pipeline covers all nine stages the assignment specifies.

    Documents → Parsing → Chunking → Embeddings → Azure AI Search
              → Retrieval / Reranking → Context → LLM → Grounded Answer + Citations

    python scripts/verify_pipeline.py                  # local backend + Azure contract test
    python scripts/verify_pipeline.py --backend azure  # run the whole pipeline on the Azure adapter
    python scripts/verify_pipeline.py --skip-azure     # local only, no stub

Stage 5 is exercised against an offline stub of the Azure AI Search REST API
(`scripts/_azure_search_stub.py`) so it is covered by the default command with
no network and no Azure spend. That is a **contract** test -- it proves the
adapter speaks the documented API, not that the documented API behaves as I
read it. See docs/pipeline.md for the live checklist.

Runs against a throwaway copy of the corpus in a temp directory, so the working
index is untouched. Exits non-zero on any failure, so it can gate CI.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _azure_search_stub import AzureSearchStub                        # noqa: E402

STAGES = [
    "1 Documents", "2 Parsing", "3 Chunking", "4 Embeddings",
    "5 Azure AI Search", "6 Retrieval / Reranking", "7 Context",
    "8 LLM", "9 Grounded Answer + Citations",
]

_results: dict[str, list[tuple[bool, str, str]]] = {s: [] for s in STAGES}
_notes: dict[str, str] = {}


def check(stage: str, label: str, ok: bool, detail: str = "") -> bool:
    _results[stage].append((bool(ok), label, detail))
    mark = "  ok " if ok else "  FAIL"
    print(f"{mark}  {label}" + (f"  — {detail}" if detail else ""))
    return bool(ok)


def note(stage: str, text: str) -> None:
    _notes[stage] = text


def heading(stage: str) -> None:
    print(f"\n{stage}\n{'-' * 62}")


# ===========================================================================


def stage_1_documents(source: Path):
    from rag.ingest.manifest import Manifest
    from rag.ingest.parsers import SUPPORTED_EXTENSIONS

    stage = STAGES[0]
    heading(stage)

    manifest = Manifest(source.parent / "manifest.json")
    changes = manifest.scan(source)

    on_disk = [p for p in source.rglob("*")
               if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS]
    departments = {p.relative_to(source).parts[0] for p in on_disk}
    extensions = {p.suffix.lower() for p in on_disk}

    check(stage, "every document classified",
          len(changes.new) == len(on_disk) and not changes.unchanged,
          f"{len(changes.new)} new of {len(on_disk)} on disk")
    check(stage, "departments discovered from the folder tree",
          len(departments) >= 5, ", ".join(sorted(departments)))
    check(stage, "all three source formats present",
          {".pdf", ".docx", ".xlsx"} <= extensions,
          " ".join(sorted(extensions)))
    check(stage, "content hash recorded per document",
          all(len(changes.hashes.get(d, "")) == 64 for d in changes.new),
          "sha256")
    note(stage, f"{len(on_disk)} documents · {len(departments)} departments")
    return changes


def stage_2_parsing(source: Path, changes):
    from rag.ingest.parsers import parse_document

    stage = STAGES[1]
    heading(stage)

    parsed = {}
    tables = 0
    empty = []
    naive_differs = 0

    for doc_id in changes.new:
        doc = parse_document(changes.paths[doc_id])
        parsed[doc_id] = doc
        tables += doc.table_count
        if not doc.blocks:
            empty.append(doc_id)
        structured = "\n".join(b.text for b in doc.blocks)
        if structured.strip() != doc.naive_text.strip():
            naive_differs += 1

    check(stage, "every document produced blocks", not empty,
          "all parsed" if not empty else f"empty: {empty}")
    check(stage, "table structure recovered", tables >= 20,
          f"{tables} table blocks")
    check(stage, "structured parse differs from the naive dump",
          naive_differs == len(parsed),
          f"{naive_differs}/{len(parsed)} documents — Scenario 1 fix is live")
    check(stage, "titles extracted",
          all(d.title and d.title.strip() for d in parsed.values()))
    note(stage, f"{sum(len(d.blocks) for d in parsed.values())} blocks · "
                f"{tables} tables")
    return parsed


def stage_3_chunking(source: Path, changes, parsed):
    from rag.ingest.chunker import chunk_document
    from rag.ingest.sync import _parse_one

    stage = STAGES[2]
    heading(stage)

    chunks, metas = [], {}
    for doc_id in changes.new:
        _, meta, _ = _parse_one(doc_id, changes.paths[doc_id], source,
                                changes.hashes[doc_id])
        metas[doc_id] = meta
        chunks.extend(chunk_document(parsed[doc_id], meta))

    # Front matter has no section by design, so it carries the title alone;
    # every chunk that *does* sit under a heading must carry "Title > Section".
    titled = sum(1 for c in chunks if c.embed_text.startswith(c.title))
    sectioned = [c for c in chunks if c.section_path != "Front matter"]
    with_breadcrumb = sum(
        1 for c in sectioned
        if c.embed_text.startswith(f"{c.title} > {c.section_path}")
    )
    table_chunks = [c for c in chunks if c.content_type == "table"]
    # A table leaking into a prose chunk means the chunker split one.
    leaked = [c for c in chunks
              if c.content_type == "text" and "| ---" in c.text.replace("|---", "| ---")]

    # Idempotency: same input must yield the same ids.
    again = []
    for doc_id in changes.new:
        again.extend(chunk_document(parsed[doc_id], metas[doc_id]))
    stable = [c.chunk_id for c in chunks] == [c.chunk_id for c in again]

    check(stage, "every chunk is prefixed with its document title",
          titled == len(chunks), f"{titled}/{len(chunks)}")
    check(stage, "every sectioned chunk carries a Title > Section breadcrumb",
          with_breadcrumb == len(sectioned),
          f"{with_breadcrumb}/{len(sectioned)} "
          f"({len(chunks) - len(sectioned)} front-matter chunks carry the title only)")
    check(stage, "tables kept whole in their own chunks",
          bool(table_chunks) and not leaked,
          f"{len(table_chunks)} table chunks, {len(leaked)} split")
    check(stage, "chunk ids are deterministic", stable,
          "same ids on re-chunk")
    check(stage, "document metadata denormalised onto chunks",
          all(c.department and c.doc_id for c in chunks))
    note(stage, f"{len(chunks)} chunks · {len(table_chunks)} tables")
    return chunks


async def stage_4_embeddings(settings, chunks):
    from rag.observability.tracing import current_trace
    from rag.providers.embeddings import get_embedding_provider

    stage = STAGES[3]
    heading(stage)

    embedder = await get_embedding_provider(settings)
    sample = [c.embed_text for c in chunks[:8]]

    before = (current_trace() or {}).get("cache_hits", 0)
    vectors = await embedder.embed(sample)
    fresh_hits = (current_trace() or {}).get("cache_hits", 0) - before

    dimensions = {len(v) for v in vectors}
    norms = [math.sqrt(sum(x * x for x in v)) for v in vectors]

    before = (current_trace() or {}).get("cache_hits", 0)
    await embedder.embed(sample)
    cached_hits = (current_trace() or {}).get("cache_hits", 0) - before

    check(stage, "one vector per chunk", len(vectors) == len(sample),
          f"{len(vectors)} vectors")
    check(stage, "vector width matches the provider",
          dimensions == {embedder.dimensions}, f"{dimensions} dims")
    check(stage, "vectors are unit-normalised",
          all(abs(n - 1.0) < 1e-6 for n in norms),
          f"min={min(norms):.6f} max={max(norms):.6f}")
    check(stage, "repeat embedding is served from cache",
          cached_hits == len(sample) and fresh_hits < len(sample),
          f"first pass {fresh_hits} hits, second pass {cached_hits}")

    live = "azure-openai" in embedder.name
    note(stage, f"provider={embedder.name} ({'Azure' if live else 'local fallback'})"
                f" · {embedder.dimensions} dims")
    if not live:
        print("  ~~ note: the Azure embedder is configured but no embedding "
              "deployment exists, so the local fallback is active")
    return embedder


async def stage_5_azure_search(chunks, embedder):
    """Exercise AzureAISearchStore against the offline REST stub."""
    from rag.config import get_settings
    from rag.ingest.manifest import Manifest, ManifestEntry
    from rag.ingest.sync import sync
    from rag.store.azure_search import AzureAISearchStore
    from rag.store.base import SearchFilters
    from rag.store.factory import get_backend

    stage = STAGES[4]
    heading(stage)

    # Sample across departments, not the first N chunks. Taking chunks[:40]
    # yields only Finance and HR (the corpus is walked alphabetically), which
    # makes the security-filter assertion below pass vacuously on zero hits --
    # a test that cannot fail is worse than no test.
    by_department: dict[str, list] = {}
    for chunk in chunks:
        by_department.setdefault(chunk.department, []).append(chunk)
    sample = [c for group in by_department.values() for c in group[:8]]
    vectors = await embedder.embed([c.embed_text for c in sample])

    with AzureSearchStub() as stub:
        os.environ["AZURE_SEARCH_ENDPOINT"] = stub.endpoint
        os.environ["AZURE_SEARCH_API_KEY"] = stub.api_key
        os.environ["AZURE_SEARCH_INDEX"] = "verify-kb"
        os.environ["AZURE_SEARCH_SEMANTIC"] = "true"
        store = AzureAISearchStore(get_settings(refresh=True))

        # --- index definition -------------------------------------------
        await store.ensure_index(embedder.dimensions)
        definition = stub.index("verify-kb").definition
        fields = {f["name"]: f for f in definition.get("fields", [])}
        vector_field = fields.get("content_vector", {})
        profiles = definition.get("vectorSearch", {}).get("profiles", [])
        algorithms = definition.get("vectorSearch", {}).get("algorithms", [])
        semantic = definition.get("semantic", {}).get("configurations", [])

        check(stage, "index created with chunk_id as key",
              fields.get("chunk_id", {}).get("key") is True)
        check(stage, "vector field sized to the embedding model",
              vector_field.get("dimensions") == embedder.dimensions
              and vector_field.get("searchable") is True,
              f"{vector_field.get('dimensions')} dims")
        check(stage, "HNSW profile wired to an algorithm",
              bool(profiles) and bool(algorithms)
              and profiles[0]["algorithm"] == algorithms[0]["name"],
              algorithms[0].get("kind", "?") if algorithms else "none")
        check(stage, "semantic configuration declared", bool(semantic),
              semantic[0]["name"] if semantic else "none")
        check(stage, "metadata fields are filterable",
              len([f for f in fields.values() if f.get("filterable")]) >= 10,
              f"{len([f for f in fields.values() if f.get('filterable')])} fields")

        # --- upsert ------------------------------------------------------
        written = await store.upsert(sample, vectors)
        stored = stub.index("verify-kb").docs
        check(stage, "documents upserted", written == len(sample)
              and len(stored) == len(sample), f"{len(stored)} documents")
        check(stage, "vectors stored alongside content",
              all(d.get("content_vector") for d in stored.values()))
        check(stage, "count endpoint agrees",
              (await store.stats()).chunks == len(sample))

        # --- hybrid + semantic query ------------------------------------
        query = "minimum password length"
        hits = await store.search(query, (await embedder.embed([query]))[0],
                            SearchFilters(), 5)
        body = (stub.last("/docs/search") or {}).get("body", {})
        check(stage, "hybrid query carries both keyword and vector",
              bool(body.get("search")) and bool(body.get("vectorQueries")),
              "search + vectorQueries in one request")
        check(stage, "semantic ranking requested",
              body.get("queryType") == "semantic")
        check(stage, "results parsed into Hits", bool(hits)
              and hits[0].chunk.chunk_id in stored,
              f"{len(hits)} hits, top={hits[0].chunk.title[:28] if hits else '-'}")
        check(stage, "reranker score mapped onto the 0-10 scale",
              all(h.rerank_score is None or 0.0 <= h.rerank_score <= 10.0
                  for h in hits))

        # --- security trimming inside the query -------------------------
        # Query with terms that match a *different* department, so a broken
        # filter would surface as leaked results rather than as no results.
        leak_probe = "pricing discount seat tier"
        unscoped = await store.search(leak_probe, (await embedder.embed([leak_probe]))[0],
                                SearchFilters(), 10)
        scoped = await store.search(leak_probe, (await embedder.embed([leak_probe]))[0],
                              SearchFilters(departments=["Sales"]), 10)
        filter_body = (stub.last("/docs/search") or {}).get("body", {})

        check(stage, "department filter emitted as OData",
              "search.in(department" in (filter_body.get("filter") or ""),
              filter_body.get("filter", "")[:52])
        check(stage, "unscoped query does reach other departments",
              any(h.chunk.department != "Sales" for h in unscoped),
              f"{len(unscoped)} hits across "
              f"{len({h.chunk.department for h in unscoped})} departments")
        check(stage, "scoped query returns Sales results and only Sales",
              bool(scoped) and all(h.chunk.department == "Sales" for h in scoped),
              f"{len(scoped)} hits, all Sales")

        # --- patch without re-embedding ---------------------------------
        target = sample[0].doc_id
        target_ids = {c.chunk_id for c in sample if c.doc_id == target}
        patched = await store.patch_document_fields(target, {"is_current": False})
        after = stub.index("verify-kb").docs
        check(stage, "metadata patched in place", patched == len(target_ids)
              and all(after[i]["is_current"] is False for i in target_ids),
              f"{patched} chunks")
        check(stage, "patch preserved vectors (no re-embedding)",
              all(after[i].get("content_vector") for i in target_ids),
              "merge left content_vector intact")

        # --- delete by document -----------------------------------------
        before_count = len(after)
        removed = await store.delete_by_doc(target)
        remaining = stub.index("verify-kb").docs
        check(stage, "delete removed exactly that document's chunks",
              removed == len(target_ids)
              and len(remaining) == before_count - len(target_ids)
              and not (target_ids & set(remaining)),
              f"{removed} removed, {len(remaining)} left")
        check(stage, "other documents untouched",
              all(c.chunk_id in remaining for c in sample
                  if c.doc_id != target))

        # --- regression: purge must not precede index creation ----------
        # sync() used to purge modified documents *before* calling
        # ensure_index. delete_by_doc searches the index to find the chunk ids
        # to remove, so on a first run against a real service that search hit an
        # index which did not exist yet and 404'd -- ingestion died before it
        # could create the index it was about to fill. What triggers it is the
        # manifest already listing the document: `--force` guarantees that, and
        # so does pointing an existing local manifest at a new Azure index.
        #
        # Reproduced with one document and a stale manifest entry, so the purge
        # path genuinely runs. Asserting on request *order* rather than "did it
        # raise" keeps the check meaningful if delete_by_doc ever learns to
        # tolerate a 404 by itself.
        fresh_index = "firstrun-kb"
        scratch = Path(tempfile.mkdtemp(prefix="rag-firstrun-"))
        saved_data_dir = os.environ.get("RAG_DATA_DIR", "")
        saved_backend = os.environ.get("RETRIEVER_BACKEND", "")
        try:
            # get_backend() reads this; without it the sync would quietly run on
            # the local store and the assertion would pass on zero requests.
            os.environ["RETRIEVER_BACKEND"] = "azure"
            one_doc = scratch / "corpus" / "Sales"
            one_doc.mkdir(parents=True)
            shutil.copy2(
                REPO_ROOT / "KnwoledgeBaseDocuments" / "Sales" / "Pricing2026.pdf",
                one_doc,
            )

            os.environ["RAG_DATA_DIR"] = str(scratch / "data")
            os.environ["AZURE_SEARCH_INDEX"] = fresh_index
            fresh_settings = get_settings(refresh=True)
            fresh_backend = await get_backend(fresh_settings)

            # A stale hash makes scan() classify the document as *modified*,
            # which is what sends it through the purge loop.
            stale = Manifest(scratch / "data" / "manifest.json")
            stale.entries["Sales/Pricing2026.pdf"] = ManifestEntry(
                doc_id="Sales/Pricing2026.pdf",
                path="Sales/Pricing2026.pdf",
                content_hash="stale-hash-forces-a-purge",
                size=1,
                chunk_ids=["chunk-that-no-longer-exists"],
            )

            mark = len(stub.requests)
            failure = ""
            try:
                await sync(fresh_settings, fresh_backend, embedder,
                           source_dir=scratch / "corpus", manifest=stale)
            except RuntimeError as exc:
                # This is what the regression looks like: the purge 404s on an
                # index that has not been created yet. Report it as a failed
                # check rather than letting it abort the remaining stages.
                failure = str(exc)[:90]
            seq = [f"{r['method']} {r['path']}" for r in stub.requests[mark:]]
            put_at = next((i for i, p in enumerate(seq)
                           if p == f"PUT /indexes/{fresh_index}"), None)
            search_at = next((i for i, p in enumerate(seq)
                              if p == f"POST /indexes/{fresh_index}/docs/search"), None)
            check(stage, "index created before any purge on a first run",
                  not failure and put_at is not None
                  and (search_at is None or put_at < search_at),
                  failure or f"PUT at {put_at}, first docs/search at {search_at}")
        finally:
            os.environ["AZURE_SEARCH_INDEX"] = "verify-kb"
            if saved_data_dir:
                os.environ["RAG_DATA_DIR"] = saved_data_dir
            if saved_backend:
                os.environ["RETRIEVER_BACKEND"] = saved_backend
            else:
                os.environ.pop("RETRIEVER_BACKEND", None)
            shutil.rmtree(scratch, ignore_errors=True)

    # --- semantic unavailable (Free tier) ------------------------------
    with AzureSearchStub(reject_semantic=True) as strict:
        os.environ["AZURE_SEARCH_ENDPOINT"] = strict.endpoint
        os.environ["AZURE_SEARCH_API_KEY"] = strict.api_key
        degraded = AzureAISearchStore(get_settings(refresh=True))
        await degraded.ensure_index(embedder.dimensions)
        await degraded.upsert(sample[:10], vectors[:10])
        query = "password"
        results = await degraded.search(query, (await embedder.embed([query]))[0],
                                  SearchFilters(), 5)
        retried = strict.calls("/docs/search")
        check(stage, "degrades gracefully when semantic ranking is unavailable",
              bool(results) and any(r["body"].get("queryType") != "semantic"
                                    for r in retried),
              "retried without queryType, kept serving")

    note(stage, "verified against an offline REST stub (contract test)")


async def stage_6_to_9(service, question: str, expected_doc: str):
    from rag.generate.guardrails import check_numeric_grounding

    started = time.perf_counter()
    answer = await service.ask(question, use_cache=False)
    elapsed = (time.perf_counter() - started) * 1000
    trace = answer.trace
    stages = {s["name"]: s for s in trace.get("stages", [])}

    # ---- 6 Retrieval / Reranking -------------------------------------
    stage = STAGES[5]
    heading(stage)
    docs = [h.chunk.doc_id for h in answer.hits]
    signals = [h for h in answer.hits
               if h.rrf_score is not None or h.vector_score is not None
               or h.keyword_score is not None]

    check(stage, "expected document retrieved", expected_doc in docs[:5],
          f"top: {docs[0] if docs else '-'}")
    check(stage, "candidates were reranked",
          all(h.rerank_score is not None for h in answer.hits),
          f"method={trace.get('rerank_method')}")
    check(stage, "retrieval signals recorded per hit",
          len(signals) == len(answer.hits),
          "rrf / vector / keyword present")
    check(stage, "search and rerank are distinct timed stages",
          "search" in stages and "rerank" in stages,
          f"search={stages.get('search', {}).get('ms')}ms "
          f"rerank={stages.get('rerank', {}).get('ms')}ms")
    note(stage, f"{len(answer.hits)} chunks · {trace.get('rerank_method')}")

    # ---- 7 Context ----------------------------------------------------
    stage = STAGES[6]
    heading(stage)
    context_stage = stages.get("context", {})
    check(stage, "context assembly is its own stage", bool(context_stage),
          f"{context_stage.get('ms')}ms")
    check(stage, "sources numbered for citation",
          context_stage.get("sources", 0) > 0,
          f"{context_stage.get('sources')} numbered sources")
    check(stage, "context stays within the character budget",
          0 < context_stage.get("chars", 0) <= context_stage.get(
              "budget_chars", 1),
          f"{context_stage.get('chars')} / "
          f"{context_stage.get('budget_chars')} chars")
    note(stage, f"{context_stage.get('sources')} sources · "
                f"{context_stage.get('chars')} chars")

    # ---- 8 LLM ---------------------------------------------------------
    stage = STAGES[7]
    heading(stage)
    tokens = trace.get("tokens", {})
    generator = trace.get("generator")
    used_llm = generator == "llm"

    check(stage, "generation stage ran", "generate" in stages,
          f"{stages.get('generate', {}).get('ms')}ms")
    if used_llm:
        check(stage, "model returned a completion with real usage",
              tokens.get("prompt", 0) > 0 and tokens.get("completion", 0) > 0,
              f"{tokens.get('prompt')}+{tokens.get('completion')} tokens")
        check(stage, "auxiliary calls accounted for",
              trace.get("llm_calls", 0) >= 1,
              f"{trace.get('llm_calls')} model calls")
    else:
        check(stage, "extractive fallback engaged and reported",
              generator == "extractive",
              "no chat deployment reachable — reported, not hidden")
    note(stage, f"{trace.get('providers', {}).get('llm', '?')} · generator={generator}")

    # ---- 9 Grounded Answer + Citations ---------------------------------
    stage = STAGES[8]
    heading(stage)
    context_ids = {h.chunk.chunk_id for h in answer.hits}
    grounded = trace.get("groundedness", {})

    check(stage, "answer produced with a known status",
          answer.status in {"answered", "insufficient_evidence",
                            "needs_clarification", "denied"},
          answer.status)
    if answer.status == "answered":
        check(stage, "answer carries citations", bool(answer.citations),
              f"{len(answer.citations)} citations")
        check(stage, "every citation resolves to a chunk in the context",
              all(c.chunk_id in context_ids for c in answer.citations))
        check(stage, "figures in the answer appear in the cited sources",
              not check_numeric_grounding(answer.text, answer.hits),
              "numeric grounding check")
        check(stage, "groundedness verified after generation",
              grounded.get("score") is not None,
              f"score={grounded.get('score')} via {grounded.get('method')}")
        check(stage, "confidence computed", answer.confidence > 0,
              f"{answer.confidence:.2f}")
    note(stage, f"status={answer.status} · confidence={answer.confidence:.2f} "
                f"· {elapsed:.0f}ms")

    print(f"\n  Q: {question}")
    print(f"  A: {' '.join(answer.text.split())[:180]}")
    print("  stages: " + " · ".join(s["name"] for s in trace.get("stages", [])))
    return answer


# ===========================================================================


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["local", "azure"], default="local",
                        help="backend for stages 6-9 (stage 5 always uses the stub)")
    parser.add_argument("--skip-azure", action="store_true",
                        help="skip the Azure AI Search contract test entirely")
    parser.add_argument("--question",
                        default="What is the minimum password length required?")
    parser.add_argument("--expect", default="IT/PasswordPolicy.docx")
    parser.add_argument("--log-level", default="ERROR")
    args = parser.parse_args()

    from rag.config import get_settings
    from rag.ingest.sync import sync
    from rag.observability.tracing import (configure_logging, set_correlation_id,
                                           start_trace)
    from rag.providers.embeddings import get_embedding_provider
    from rag.service import AssistantService
    from rag.store.factory import get_backend

    configure_logging(args.log_level, "text")

    workdir = Path(tempfile.mkdtemp(prefix="rag-verify-"))
    source = workdir / "corpus"
    shutil.copytree(REPO_ROOT / "KnwoledgeBaseDocuments", source)

    os.environ["RAG_SOURCE_DIR"] = str(source)
    os.environ["RAG_DATA_DIR"] = str(workdir / "data")
    os.environ["RAG_PROFILE"] = "improved"
    os.environ["RETRIEVER_BACKEND"] = "local"
    settings = get_settings(refresh=True)

    set_correlation_id(None)
    start_trace()

    print("\n" + "=" * 62)
    print("  RAG pipeline — nine-stage verification")
    print("=" * 62)
    print(f"  corpus:  {source}")
    print(f"  backend: {args.backend} (stages 6-9)")

    try:
        changes = stage_1_documents(source)
        parsed = stage_2_parsing(source, changes)
        chunks = stage_3_chunking(source, changes, parsed)
        embedder = await stage_4_embeddings(settings, chunks)

        if args.skip_azure:
            heading(STAGES[4])
            print("  -- skipped (--skip-azure)")
            note(STAGES[4], "skipped")
        else:
            await stage_5_azure_search(chunks, embedder)

        # Build a live index for stages 6-9 through the real sync pipeline.
        if args.backend == "azure":
            stub = AzureSearchStub().start()
            os.environ["RETRIEVER_BACKEND"] = "azure"
            os.environ["AZURE_SEARCH_ENDPOINT"] = stub.endpoint
            os.environ["AZURE_SEARCH_API_KEY"] = stub.api_key
            os.environ["AZURE_SEARCH_INDEX"] = "pipeline-kb"
        else:
            stub = None
            os.environ["RETRIEVER_BACKEND"] = "local"

        settings = get_settings(refresh=True)
        backend = await get_backend(settings)
        embedder = await get_embedding_provider(settings)
        report = await sync(settings, backend, embedder, source_dir=source)
        print(f"\n  indexed {report.total_chunks} chunks "
              f"({report.table_chunks} tables) into {backend.name}")

        service = await AssistantService.create(settings)
        await stage_6_to_9(service, args.question, args.expect)

        if stub is not None:
            stub.stop()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    # ---- summary --------------------------------------------------------
    print("\n" + "=" * 62)
    print(f"  {'stage':<32} {'checks':>8}  {'result':<7} notes")
    print("-" * 62)
    failed = 0
    for stage in STAGES:
        checks = _results[stage]
        bad = [c for c in checks if not c[0]]
        failed += len(bad)
        if not checks:
            verdict, count = "SKIP", "-"
        else:
            verdict = "PASS" if not bad else "FAIL"
            count = f"{len(checks) - len(bad)}/{len(checks)}"
        print(f"  {stage:<32} {count:>8}  {verdict:<7} {_notes.get(stage, '')}")
    print("=" * 62)

    covered = sum(1 for s in STAGES
                  if _results[s] and all(c[0] for c in _results[s]))
    print(f"\n  {covered}/{len(STAGES)} stages verified, "
          f"{sum(len(v) for v in _results.values())} checks, {failed} failure(s)")
    if not args.skip_azure:
        print("  Stage 5 is a contract test against an offline REST stub, not a\n"
              "  live integration test. See docs/pipeline.md for the live checklist.")
    return 1 if failed else 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
