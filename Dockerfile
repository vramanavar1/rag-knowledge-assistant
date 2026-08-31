# syntax=docker/dockerfile:1
#
# Container image for the Northwind Knowledge Assistant API + UI.
#
#   docker build -t rag-assistant:demo --build-arg BAKE_INDEX=true  .   # self-contained
#   docker build -t rag-assistant:prod --build-arg BAKE_INDEX=false .   # Azure-backed
#   docker run --rm -p 8000:8000 rag-assistant:demo
#
# Distinct tags on purpose: the two variants are not interchangeable, and pushing
# the wrong one ships a stale baked index to production. Deployment.md §6.1.
#
# NOTE: BAKE_INDEX controls only whether the *index* is pre-built. The corpus
# itself is COPYed into the runtime stage either way, so the documents are in
# both images. Deployment.md §6.3 covers the alternatives.
#
# Verified on the 3.14 base: the baked image is 342 MB, reports Python 3.14.7,
# runs as uid 10001, serves /health 200 with 11 documents / 127 chunks, answers
# through the API, and refuses unauthenticated requests with 401. The
# BAKE_INDEX=false variant (verified on the 3.13 base) builds and correctly
# reports 503 until a backend is configured.

ARG PYTHON_VERSION=3.14

# ---------------------------------------------------------------------------
# 1. Dependencies, resolved once into a virtualenv.
#
# All seven dependencies ship manylinux wheels, so the slim base needs no
# compiler. If a wheel is ever missing for your platform this stage is where it
# fails, loudly, rather than at runtime.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS deps

# PIP_PROGRESS_BAR=off is about the *log*, not the build. pip draws its progress
# bar with U+2501 (heavy horizontal), and a build log carrying that character
# kills `az acr build` on a default Windows console -- cp437/cp1252 cannot encode
# it, so the CLI's log printer raises UnicodeEncodeError and the command appears
# to fail while the ACR task is still happily running. Plain ASCII output costs
# nothing here and cannot trip any console.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PIP_PROGRESS_BAR=off

WORKDIR /app
COPY requirements.txt ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt


# ---------------------------------------------------------------------------
# 2. Optionally bake the search index into the image.
#
# `data/` is gitignored, so a clean checkout has no index and a container built
# without one would start up and correctly fail its readiness probe. Baking at
# build time makes the demo image self-contained and start instantly.
#
# Set BAKE_INDEX=false for the production image: with RETRIEVER_BACKEND=azure
# the index lives in Azure AI Search, and a baked local index would be dead
# weight built with whichever embedding provider happened to be configured here.
# ---------------------------------------------------------------------------
FROM deps AS index

ARG BAKE_INDEX=true
ENV PATH=/opt/venv/bin:$PATH

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY KnwoledgeBaseDocuments/ ./KnwoledgeBaseDocuments/

RUN if [ "$BAKE_INDEX" = "true" ]; then \
        echo "baking index (local embedder — no credentials at build time)" && \
        python scripts/ingest.py --log-format text ; \
    else \
        echo "skipping index bake (BAKE_INDEX=false)" && mkdir -p data ; \
    fi


# ---------------------------------------------------------------------------
# 3. Runtime.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    RAG_PROFILE=improved \
    RETRIEVER_BACKEND=local \
    LOG_FORMAT=json \
    LOG_LEVEL=INFO \
    API_ALLOW_ANONYMOUS=false

# Non-root. `data/` is owned by the app user because POST /api/v1/ingest writes
# there; everything else is read-only to the process.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app
COPY --from=deps /opt/venv /opt/venv
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser eval/ ./eval/
COPY --chown=appuser:appuser KnwoledgeBaseDocuments/ ./KnwoledgeBaseDocuments/
COPY --chown=appuser:appuser --from=index /app/data ./data

USER appuser
EXPOSE 8000

# For `docker run`. Azure Container Apps ignores this and uses its own probes —
# see Deployment.md §6.7 for why /health must be a readiness probe only.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,httpx; sys.exit(0 if httpx.get('http://127.0.0.1:8000/health', timeout=4).status_code == 200 else 1)"

# --host 0.0.0.0 is required: uvicorn's default binds loopback only, which would
# make the container unreachable through any ingress.
CMD ["uvicorn", "rag.api.app:app", "--app-dir", "src", \
     "--host", "0.0.0.0", "--port", "8000"]
