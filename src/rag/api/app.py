"""FastAPI application.

    uvicorn rag.api.app:app --app-dir src --reload --port 8000

Endpoints
    GET  /                     the chat UI
    GET  /health               readiness probe (index loaded, providers resolved)
    POST /api/v1/chat          ask a question
    GET  /api/v1/documents     what is indexed, with version status
    POST /api/v1/ingest        re-run incremental ingestion (admin)
    GET  /api/v1/principals    demo tokens, so the UI can build its role picker
    GET  /docs                 OpenAPI
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from rag.api.auth import available_tokens, require_admin, resolve_principal
from rag.api.schemas import (
    ChatRequest,
    ChatResponse,
    DocumentModel,
    ErrorResponse,
    IngestRequest,
    IngestResponse,
)
from rag.config import get_settings
from rag.ingest.manifest import Manifest
from rag.ingest.sync import sync
from rag.models import Principal, Turn
from rag.observability.tracing import (
    configure_logging,
    get_correlation_id,
    get_logger,
    set_correlation_id,
)
from rag.service import AssistantService

log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"

_service: AssistantService | None = None


def get_service() -> AssistantService:
    if _service is None:  # pragma: no cover - lifespan guarantees this
        raise RuntimeError("service not initialised")
    return _service


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    log.info("starting api", profile=settings.profile,
             backend=settings.retriever_backend)
    _service = await AssistantService.create(settings)
    if (await _service.backend.stats()).chunks == 0:
        log.warning(
            "index is empty - run `python scripts/ingest.py` before asking questions"
        )
    yield
    # Connection pools outlive the process otherwise, and an unclosed
    # AsyncClient leaks its sockets.
    await _service.aclose()
    log.info("shutting down api")


app = FastAPI(
    title="Northwind Traders Knowledge Assistant",
    description=(
        "RAG assistant over the Northwind Traders enterprise document set. "
        "Answers are grounded in retrieved passages, cited by source number, "
        "verified for groundedness, and trimmed to the caller's department."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

settings = get_settings()

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["X-Correlation-Id"],
)


@app.middleware("http")
async def observability_and_headers(request: Request, call_next):
    """Correlation id, access log, and the security headers a browser needs."""
    correlation_id = set_correlation_id(
        request.headers.get("X-Correlation-Id")
        or request.headers.get("X-Request-Id")
    )
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error", path=request.url.path,
                      method=request.method)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(
                error="internal_error",
                detail="An unexpected error occurred. Quote the correlation id "
                       "when reporting this.",
                correlation_id=correlation_id,
            ).model_dump(),
            headers={"X-Correlation-Id": correlation_id},
        )

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Correlation-Id"] = correlation_id
    # The page is entirely self-contained, so the CSP can be strict.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"

    if not request.url.path.startswith("/static"):
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
    return response


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


# NOTE ON `async def` BELOW.
#
# The handlers are `async def` and they genuinely await: `service.ask()` is a
# coroutine all the way down to `httpx.AsyncClient`. A request that is waiting
# on Azure OpenAI or Azure AI Search -- which is ~95% of its ~5s life -- holds a
# coroutine, not a thread, so a replica's concurrency ceiling is set by memory
# rather than by a ~40-worker threadpool.
#
# This is the third state of this code, and the middle one is the trap:
#
#   1. `async def` over blocking calls  -- ran ON the loop, one request at a
#      time. Measured: 4 concurrent requests took 4.56x a single request.
#   2. plain `def` over blocking calls  -- FastAPI's anyio threadpool. Correct,
#      and the right fix while the internals were still synchronous, but capped
#      at ~40 in-flight requests. Measured: /health p95 hit 641ms under 60
#      concurrent requests, because probes queued behind saturated workers.
#   3. `async def` over awaited calls   -- what this is now.
#
# State 1 and state 3 look identical at the signature. What makes state 3 safe
# is that nothing underneath blocks: work with no network in it (local BM25
# scoring, feature hashing, PDF parsing, index writes) is dispatched with
# `asyncio.to_thread` rather than merely relabelled `async`.
#
# The middleware was always `async`, because it really does await `call_next`.


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "chat.html")


@app.get("/health", tags=["ops"], summary="Readiness probe")
async def health() -> JSONResponse:
    """Reports ready only when an index is actually loaded.

    A probe that returns 200 because the process is up would let an instance
    with an empty index take traffic and answer every question with
    "I don't know".

    Setting ``SHOW_ENV_VALUES=true`` adds an ``env`` block listing every
    variable the app reads, including ones that are unset -- which is what makes
    a missing or unresolved setting visible. Secret values are always redacted
    to a length and hash fingerprint, whether or not the flag is on.
    """
    report = await get_service().health()
    code = (status.HTTP_200_OK if report["status"] == "ready"
            else status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse(status_code=code, content=report)


@app.post("/api/v1/chat", response_model=ChatResponse, tags=["chat"])
async def chat(
    request: ChatRequest,
    principal: Principal = Depends(resolve_principal),
) -> ChatResponse:
    """Answer a question, grounded in the documents this caller may read."""
    service = get_service()

    # Refuse rather than attempt. Without this the request reaches the retriever
    # and dies deep in the search backend on a vector-length mismatch -- a 500
    # naming neither the cause nor the fix. 503 is the honest code: the service
    # is configured in a way it cannot serve.
    if service.startup_errors:
        first = service.startup_errors[0]
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(f"{first['check']}: {first['reason']}. Fix: {first['fix']}. "
                    f"See /health for the full startup report."),
        )

    history = [Turn(role=t.role, content=t.content) for t in request.history]

    answer = await service.ask(
        request.question,
        history=history,
        principal=principal,
        use_cache=request.use_cache,
    )
    return ChatResponse.from_answer(
        answer,
        get_correlation_id(),
        include_trace=request.include_trace,
        include_hits=request.include_hits,
    )


@app.get("/api/v1/documents", response_model=list[DocumentModel], tags=["corpus"])
async def documents(
    principal: Principal = Depends(resolve_principal),
) -> list[DocumentModel]:
    """List indexed documents visible to the caller."""
    service = get_service()
    # Reading the manifest is a blocking file read; it belongs off the loop.
    manifest = await asyncio.to_thread(
        lambda: Manifest(service.settings.manifest_path()).load()
    )

    result: list[DocumentModel] = []
    for entry in sorted(manifest.entries.values(), key=lambda e: e.doc_id):
        meta = entry.document_meta()
        if not principal.can_see(meta.department):
            continue
        result.append(
            DocumentModel(
                doc_id=meta.doc_id,
                title=meta.title,
                department=meta.department,
                doc_type=meta.doc_type,
                version=meta.version,
                effective_date=meta.effective_date,
                is_current=meta.is_current,
                superseded_by=meta.superseded_by,
                chunks=len(entry.chunk_ids),
            )
        )
    return result


@app.post("/api/v1/ingest", response_model=IngestResponse, tags=["corpus"])
async def ingest(
    request: IngestRequest,
    principal: Principal = Depends(resolve_principal),
) -> IngestResponse:
    """Run an incremental sync of the source folder into the index."""
    require_admin(principal)
    service = get_service()

    report = await sync(
        service.settings,
        service.backend,
        service.embedder,
        force=request.force,
    )
    # New or removed content invalidates cached answers.
    if report.chunks_written or report.chunks_purged:
        service.cache.clear()

    return IngestResponse(
        new=report.new,
        modified=report.modified,
        deleted=report.deleted,
        unchanged=report.unchanged,
        superseded_now=report.superseded_now,
        reinstated=report.reinstated,
        failed=report.failed,
        chunks_written=report.chunks_written,
        chunks_purged=report.chunks_purged,
        total_chunks=report.total_chunks,
        embedding_calls=report.embedding_calls,
        cache_hits=report.cache_hits,
    )


@app.get("/api/v1/principals", tags=["ops"],
         summary="Demo bearer tokens and their department scopes")
async def principals() -> dict[str, object]:
    return {
        "note": "Development tokens only. Production validates Entra ID JWTs "
                "and maps group claims to the same department scopes.",
        "tokens": available_tokens(),
    }
