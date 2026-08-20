# Deployment

How to run the Northwind Knowledge Assistant — on a laptop with nothing
installed, against real Azure services, and in production on Azure Container
Apps. Every command says what it does and why it is there.

---

## Contents

1. [What you are deploying](#1-what-you-are-deploying)
2. [Prerequisites](#2-prerequisites)
3. [Path A — local, zero-install](#3-path-a--local-zero-install)
4. [Local testing](#4-local-testing)
5. [Path B — local app, real Azure services](#5-path-b--local-app-real-azure-services)
6. [Path C — production on Azure Container Apps](#6-path-c--production-on-azure-container-apps)
7. [Configuration reference](#7-configuration-reference)
8. [Operations](#8-operations)
9. [Troubleshooting](#9-troubleshooting)
10. [What is not included](#10-what-is-not-included)

---

## 1. What you are deploying

A FastAPI service that answers questions over an enterprise document corpus,
plus a single-page chat UI it serves itself. One process, five endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /` | The chat UI |
| `GET /health` | Readiness probe — **503 when the index is empty** |
| `POST /api/v1/chat` | Ask a question |
| `GET /api/v1/documents` | What is indexed, trimmed to the caller |
| `POST /api/v1/ingest` | Re-sync the corpus (admin only) |
| `GET /docs` | OpenAPI |

### The one decision that shapes everything: which search backend

Retrieval sits behind a `SearchBackend` protocol with two implementations, and
the choice determines whether the service is stateless.

| | `RETRIEVER_BACKEND=local` (default) | `RETRIEVER_BACKEND=azure` |
|---|---|---|
| Index lives in | `data/index.{profile}.json` on local disk | Azure AI Search |
| Reranking | LLM reranker (~2 s, 1 extra model call) | The service's semantic ranker (in-request) |
| Stateless? | **No** — each replica holds its own index | **Yes** |
| Scale to >1 replica | Not meaningfully | Yes |
| Cost | Free | Search service (Basic ≈ $75/mo for the semantic ranker) |
| Good for | Local dev, CI, demos, evaluation | Production |

**Use the local backend for everything up to production, and the Azure backend
in production.** Nothing in the pipeline branches on which is active — see
[docs/pipeline.md](docs/pipeline.md).

### What is stateful, and where

Three pieces of state, and they behave differently:

- **The index** — external with the Azure backend, on disk with the local one.
- **The ingestion manifest** (`data/manifest.{profile}.json`) — **always local**,
  even in Azure mode. It records each document's content hash and the exact
  chunk ids it produced. Lose it and the next ingest re-embeds the whole corpus
  and, worse, **cannot detect deletions** — nothing remembers which documents
  used to exist. This is why ingestion must not run inside an ephemeral API
  container (§6.4).
- **The answer cache** — in-memory, per replica, scoped by department. Nothing
  is lost if it goes; hit rate just drops.

---

## 2. Prerequisites

**Required**

- Python 3.13 (3.12 works). No other runtime.
- Seven packages: `fastapi`, `uvicorn`, `pydantic`, `httpx`, `python-dotenv`,
  `PyMuPDF`, `python-docx`. If your environment already has FastAPI, you likely
  need nothing:
  ```bash
  pip install -r requirements.txt
  ```
  Azure OpenAI and Azure AI Search are called over their REST APIs with `httpx`
  rather than through the SDKs, so there is no `azure-*` dependency at all.

**Optional, for the Azure paths**

- An Azure subscription and the `az` CLI, logged in (`az login`).
- Azure OpenAI with a chat deployment; ideally also an embedding deployment and
  a small utility deployment.
- Azure AI Search, Basic SKU or above if you want the semantic ranker.
- Docker, for §6.

---

## 3. Path A — local, zero-install

The fastest way to see it work. No Azure account, no credentials, no network.

```bash
git clone <repo> && cd rag-knowledge-assistant

# 1. Build the index from the corpus in KnwoledgeBaseDocuments/
python scripts/ingest.py

# 2. Serve the API and UI
uvicorn rag.api.app:app --app-dir src --port 8000
```

Open <http://localhost:8000>.

**What step 1 does.** Walks the corpus, parses each document preserving table
structure, chunks it section-aware with heading breadcrumbs, embeds each chunk,
and writes `data/index.improved.json`. Expect:

```
profile=improved  backend=local  embeddings=local-hashing
  11 new, 0 modified, 0 deleted, 0 unchanged
  chunks: +127 written, -0 purged, 127 total (22 tables)
  embeddings: 0 API batches, 0 cache hits
  superseded: Sales/Pricing2025.pdf
```

`22 tables` is the signal that table-aware parsing worked. `superseded:
Sales/Pricing2025.pdf` is version reconciliation noticing the 2026 rate card
replaces the 2025 one.

**What you get with no credentials.** The app runs completely, and says exactly
what it is running on:

```
no embedding deployment configured, using local embedder
embedding provider active  provider=local-hashing dimensions=768
no Azure OpenAI chat deployment configured; answers will be extractive
```

Answers are extractive rather than written by a model, and retrieval uses a
hashed-feature embedder with no notion of synonymy. Both are *reported*, never
silently substituted. It still answers correctly — "What is the minimum
password length?" returns the 12-character row from the policy table in ~12 ms.

Add `AZURE_OPENAI_ENDPOINT` / `_API_KEY` / `_CHAT_DEPLOYMENT` (§5) and the same
question comes back as a written, cited, verified answer.

### From the terminal instead

```bash
python -m rag.cli ask "What is the nightly hotel cap in London?"
python -m rag.cli chat --department Sales          # multi-turn, department-scoped
python -m rag.cli compare "What was the Professional tier price in 2025?"
```

`compare` runs the question through both the naive baseline and the improved
pipeline side by side — the quickest way to see a failure scenario and its fix.

---

## 4. Local testing

Local testing is fully supported and needs no Azure account. Four suites, in
increasing cost.

### 4.1 Pipeline coverage — 53 checks, no network

```bash
python scripts/verify_pipeline.py
```

Walks all nine stages of the assignment's pipeline against a throwaway copy of
the corpus and asserts each. Expect `9/9 stages verified, 53 checks, 0
failure(s)`. Exits non-zero on failure, so it can gate CI.

**Stage 5 (Azure AI Search) is covered here with no Azure account.** It runs
against an offline stub of the Search REST API
([`scripts/_azure_search_stub.py`](scripts/_azure_search_stub.py)), proving the
index definition, hybrid + semantic query construction, OData security filters,
delete-by-document, and metadata merge without re-embedding.

To run the *whole* pipeline on the Azure adapter rather than just stage 5:

```bash
python scripts/verify_pipeline.py --backend azure
```

Expect `rerank_method=azure-semantic` and 2 model calls instead of 3 — the
service's reranker replaces the LLM one.

### 4.2 Document lifecycle — 23 assertions

```bash
python scripts/verify_lifecycle.py
```

Copies the corpus to a temp directory, then adds, modifies and deletes
documents and asserts the index tracks it: no orphan chunks after an edit, zero
embedding calls when nothing changed, and supersession that reverses when a
newer document is removed. Expect `All lifecycle checks passed.`

### 4.3 Evaluation — 35 questions, baseline vs improved

Costs Azure OpenAI tokens (~$0.35 for both profiles) and needs a chat
deployment. Skip with `--no-judge` to halve the calls.

```bash
python scripts/ingest.py --profile baseline      # build the naive "before" index
python scripts/ingest.py --profile improved

python eval/run_eval.py --profile baseline --out eval/results/baseline.json
python eval/run_eval.py --profile improved --out eval/results/improved.json
python eval/run_eval.py --compare eval/results/baseline.json \
                                  eval/results/improved.json \
                                  --report eval/results/comparison.md
```

Expect ~63% → ~97% answer correctness and 11% → 3% hallucination. A single
category runs in about a minute:

```bash
python eval/run_eval.py --profile improved --category versioning --no-judge
```

To re-apply a scoring change to saved runs without spending tokens again:

```bash
python eval/run_eval.py --rescore eval/results/baseline.json
```

### 4.4 Manual smoke — API and access control

```bash
curl -s localhost:8000/health | python -m json.tool

# a Sales caller asking an HR question gets nothing
curl -s localhost:8000/api/v1/chat \
  -H 'Authorization: Bearer demo-sales' -H 'Content-Type: application/json' \
  -d '{"question":"How many weeks of paid parental leave do I get?"}'

# the same question as HR
curl -s localhost:8000/api/v1/chat \
  -H 'Authorization: Bearer demo-hr' -H 'Content-Type: application/json' \
  -d '{"question":"How many weeks of paid parental leave do I get?"}'
```

Demo tokens: `demo-admin`, `demo-finance`, `demo-hr`, `demo-it`, `demo-legal`,
`demo-sales`. `GET /api/v1/principals` lists them.

The first call should decline; the second should answer "12 weeks" citing
`HR/LeavePolicy.pdf`. If the first one answers, the department pre-filter is
not working — that is a security regression, not a quality one.

### 4.5 Expected failures

One evaluation case, `noans-03`, fails intermittently. It asks about a
"Standard plan" that does not exist, and the 2026 rate card happens to contain
the sentence *"Standard contract term is 12 months…"* — so every grounding check
legitimately passes on the wrong entity. It is documented in
[docs/evaluation.md](docs/evaluation.md#the-one-case-that-still-fails). A run
scoring 34/35 is expected; 33/35 or lower is a regression.

---

## 5. Path B — local app, real Azure services

### 5.1 Azure OpenAI

```bash
cp .env.example .env      # then edit, or export directly
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
export AZURE_OPENAI_API_KEY=<key>
export AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o
export AZURE_OPENAI_UTILITY_DEPLOYMENT=gpt-4o-mini
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
```

Three things worth knowing:

- **The utility deployment matters more than it looks.** Reranking, query
  condensation and answer verification are ~75% of the model calls and ~78% of
  the latency. Pointing them at a mini deployment is the single largest cost and
  speed win available, and costs nothing in quality — they are scoring and
  checking tasks, not synthesis. If the deployment does not exist the app falls
  back to the chat deployment.
- **The embedding deployment is the largest quality lever.** Without it,
  retrieval runs on a hashed-feature embedder with no notion of synonymy.
- **Changing the embedding model invalidates the index.** Vector widths must
  match; the store logs `embedding dimension changed … will be dropped`. Re-run
  `python scripts/ingest.py --force`.

Verify what is live:

```bash
python -m rag.cli ask "What is the minimum password length?"
# startup log should read: chat provider active  provider=azure-openai:<deployment>
```

### 5.2 Azure AI Search

```bash
# creates the search service, and model deployments if OPENAI_NAME is set
RESOURCE_GROUP=rg-rag SEARCH_SKU=basic OPENAI_NAME=<aoai-resource> \
  bash scripts/provision_azure_search.sh

export RETRIEVER_BACKEND=azure
export AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
export AZURE_SEARCH_API_KEY=<admin-key>
export AZURE_SEARCH_INDEX=northwind-kb

python scripts/ingest.py --force        # creates the index and loads it
```

The index is created by `ingest.py` with a `PUT`, which is create-or-update, so
the schema always matches the code that queries it — there is no separate
migration step.

`SEARCH_SKU=free` works but has no semantic ranker; set
`AZURE_SEARCH_SEMANTIC=false`, or leave it and let the app degrade once, loudly,
on the first query.

**This path is contract-verified, not integration-verified.** The adapter has
been exercised against an offline stub of the REST API, not against a live
service. Run this checklist the first time you point it at real Azure:

```bash
python scripts/ingest.py --force        # expect: index created, 127 chunks
python -m rag.cli ask "What is the nightly hotel cap in London?"
#   expect: $350, and rerank_method=azure-semantic in the trace
python -m rag.cli --department Sales ask "How many weeks of parental leave do I get?"
#   expect: declined
python eval/run_eval.py --profile improved --out eval/results/improved-azure.json
#   expect: at or above the local backend's scores
```

### 5.3 Behind a TLS-inspecting corporate proxy

If every Azure call fails with `CERTIFICATE_VERIFY_FAILED: unable to get local
issuer certificate`, your network is intercepting TLS with a CA that Python's
bundled certificate store does not know. Three options, best first:

```bash
export AZURE_CA_BUNDLE=/path/to/corporate-root.pem   # correct fix
export AZURE_USE_SYSTEM_CERTS=true                   # use the OS trust store
export AZURE_TLS_VERIFY=false                        # dev only; warns on every start
```

`AZURE_TLS_VERIFY=false` disables the check that the endpoint you are sending an
API key to is really Azure. Local development only — never in a deployed
environment.

---

## 6. Path C — production on Azure Container Apps

> **What in this section has been run.** The image build and everything you can
> do with the resulting container — §6.1 — were executed and verified. The
> `az containerapp` commands from §6.2 onward were **not**: they require an Azure
> subscription this was not deployed to. They follow the documented CLI surface.
> See [§10](#10-what-is-not-included).

### 6.1 Build the image

```bash
# self-contained demo image: index baked in at build time
docker build -t rag-assistant:latest .

# production image: state lives in Azure AI Search, so skip the bake
docker build -t rag-assistant:latest --build-arg BAKE_INDEX=false .
```

`data/` is gitignored, so a clean checkout has no index — a container built
without one starts up and correctly fails its readiness probe. `BAKE_INDEX=true`
(the default) runs `scripts/ingest.py` during the build so the demo image is
self-contained and starts instantly. For the Azure-backed image a baked local
index is dead weight built with whatever embedding provider happened to be
configured at build time, hence `false`.

Test it locally before pushing:

```bash
docker run --rm -p 8000:8000 rag-assistant:latest
curl -s localhost:8000/health
```

Verified behaviour of the baked image — 337 MB, and:

```
/health              -> 200  {"status":"ready","documents":11,"chunks":127}
docker exec … id     -> uid=10001(appuser)          # non-root
POST /api/v1/chat    -> answers, extractive with no credentials passed
POST without a token -> 401                          # API_ALLOW_ANONYMOUS=false
```

The `BAKE_INDEX=false` variant builds and returns **503** until you point it at
a backend — which is the correct readiness answer for a container with no index,
not a defect.

Pass credentials at run time and the same image upgrades itself to written,
cited answers:

```bash
docker run --rm -p 8000:8000 \
  -e AZURE_OPENAI_ENDPOINT="$AZURE_OPENAI_ENDPOINT" \
  -e AZURE_OPENAI_API_KEY="$AZURE_OPENAI_API_KEY" \
  -e AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o \
  rag-assistant:latest
# /health now reports llm_available: true; answers cite sources and are verified
```

Worth knowing if your host sits behind a TLS-inspecting proxy: the container
does **not** inherit it. Calls that need `AZURE_TLS_VERIFY=false` on the host
(§5.3) succeeded from inside the Linux container with verification fully on.

### 6.2 Provision

```bash
RESOURCE_GROUP=rg-rag-prod
LOCATION=eastus

az group create -n $RESOURCE_GROUP -l $LOCATION

# Search + model deployments
RESOURCE_GROUP=$RESOURCE_GROUP SEARCH_SKU=basic OPENAI_NAME=<aoai> \
  bash scripts/provision_azure_search.sh

# Registry, Key Vault, observability, Container Apps environment
az acr create -g $RESOURCE_GROUP -n acrragprod --sku Basic --admin-enabled false
az keyvault create -g $RESOURCE_GROUP -n kv-rag-prod
az monitor app-insights component create \
  -g $RESOURCE_GROUP -a appi-rag-prod -l $LOCATION --workspace <log-analytics-id>
az containerapp env create -g $RESOURCE_GROUP -n cae-rag-prod -l $LOCATION
```

Push the image:

```bash
az acr login -n acrragprod
docker tag rag-assistant:latest acrragprod.azurecr.io/rag-assistant:1.0.0
docker push acrragprod.azurecr.io/rag-assistant:1.0.0
```

Tag with a version, not `latest` — Container Apps revisions are how you roll
back, and they need distinguishable images.

### 6.3 Load the index

Run ingestion **once, from a trusted machine**, against the production search
service:

```bash
export RETRIEVER_BACKEND=azure
export AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
export AZURE_SEARCH_API_KEY=$(az search admin-key show \
  --service-name <name> -g $RESOURCE_GROUP --query primaryKey -o tsv)
export AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=... \
       AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small

python scripts/ingest.py --force
```

### 6.4 Where ingestion runs — and where it must not

**Do not run ingestion inside the API container.** The manifest
(`data/manifest.improved.json`) is local state that records what has already
been indexed; on an ephemeral filesystem it disappears on every restart, and
with it the ability to detect deleted documents. Three options, worst to best:

1. **Manual** (above) — fine while the corpus changes rarely.
2. **A Container Apps Job** on a schedule, with the manifest on an Azure Files
   mount so it survives between runs:
   ```bash
   az containerapp job create -g $RESOURCE_GROUP -n job-rag-ingest \
     --environment cae-rag-prod --trigger-type Schedule \
     --cron-expression "0 */6 * * *" \
     --image acrragprod.azurecr.io/rag-assistant:1.0.0 \
     --command "python" --args "scripts/ingest.py"
   ```
3. **Event-driven**, as [docs/architecture.md](docs/architecture.md) specifies:
   Blob Storage → Event Grid → Queue → Function, with the manifest in Cosmos DB.
   This is the design target; it is not implemented here.

### 6.5 Deploy

```bash
az containerapp create \
  -g $RESOURCE_GROUP -n ca-rag-assistant --environment cae-rag-prod \
  --image acrragprod.azurecr.io/rag-assistant:1.0.0 \
  --registry-server acrragprod.azurecr.io \
  --system-assigned \
  --ingress external --target-port 8000 --transport auto \
  --min-replicas 1 --max-replicas 10 \
  --cpu 1.0 --memory 2.0Gi \
  --env-vars \
     RETRIEVER_BACKEND=azure \
     RAG_PROFILE=improved \
     AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net \
     AZURE_SEARCH_INDEX=northwind-kb \
     AZURE_OPENAI_ENDPOINT=https://<aoai>.openai.azure.com \
     AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o \
     AZURE_OPENAI_UTILITY_DEPLOYMENT=gpt-4o-mini \
     AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small \
     API_ALLOW_ANONYMOUS=false \
     API_ALLOWED_ORIGINS=https://chat.northwindtraders.example \
     LOG_FORMAT=json
```

`--min-replicas 1` rather than 0: scale-to-zero means the first question after
an idle period pays a cold start that includes loading the index.

**Two settings that are security-critical and easy to forget.**
`API_ALLOW_ANONYMOUS` defaults to `true` for local development — leave it and
your production endpoint answers unauthenticated requests with unrestricted
department scope. `API_ALLOWED_ORIGINS` defaults to `http://localhost:8000`;
set it to the real origin or the browser will block the UI.

### 6.6 Secrets and identity

Never pass keys with `--env-vars`; they are visible in the revision definition.

```bash
# grant the app's identity access, then reference secrets from Key Vault
PRINCIPAL=$(az containerapp show -g $RESOURCE_GROUP -n ca-rag-assistant \
  --query identity.principalId -o tsv)
az role assignment create --assignee $PRINCIPAL \
  --role "Search Index Data Reader" --scope <search-resource-id>
az role assignment create --assignee $PRINCIPAL \
  --role "Cognitive Services OpenAI User" --scope <aoai-resource-id>

az containerapp secret set -g $RESOURCE_GROUP -n ca-rag-assistant \
  --secrets search-key=keyvaultref:https://kv-rag-prod.vault.azure.net/secrets/search-key,identityref:system
az containerapp update -g $RESOURCE_GROUP -n ca-rag-assistant \
  --set-env-vars AZURE_SEARCH_API_KEY=secretref:search-key
```

The application authenticates with API keys today. Moving to Managed Identity
end-to-end means swapping the `api-key` header for a bearer token in
[`src/rag/providers/http.py`](src/rag/providers/http.py) — the role assignments
above are the half that is already correct.

### 6.7 Probes and scaling

```bash
az containerapp update -g $RESOURCE_GROUP -n ca-rag-assistant \
  --scale-rule-name concurrency --scale-rule-type http \
  --scale-rule-http-concurrency 20
```

Probe configuration, via YAML on the container:

| Probe | Use | Why |
|---|---|---|
| Readiness | `httpGet /health`, period 10 s | Returns **503 while the index is empty**, so a replica takes traffic only once it can actually answer |
| Liveness | **TCP on 8000**, not `/health` | `/health` returning 503 for an empty index is correct readiness behaviour and catastrophic liveness behaviour — it would restart-loop a healthy process forever |
| Startup | `httpGet /health`, failureThreshold 12 | Loading the index takes a few seconds |

Scaling on HTTP concurrency rather than CPU is deliberate: the service spends
most of a request waiting on Azure OpenAI, so CPU stays low while request
latency climbs, and a CPU rule would never fire.

---

## 7. Configuration reference

Every variable, its default and its effect. All are optional — with none set
the app runs fully locally and says so.

### Azure OpenAI

| Variable | Default | Effect |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | — | Resource endpoint. Unset ⇒ extractive answers |
| `AZURE_OPENAI_API_KEY` | — | API key |
| `AZURE_OPENAI_API_VERSION` | `2024-10-21` | Data-plane API version |
| `AZURE_OPENAI_CHAT_DEPLOYMENT` | falls back to `AZURE_OPENAI_DEPLOYMENT_NAME` | Answer synthesis |
| `AZURE_OPENAI_UTILITY_DEPLOYMENT` | chat deployment | Rerank / condense / verify / judge. **Point at a mini model** |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | — | Unset ⇒ local hashed-feature embedder |
| `AZURE_OPENAI_EMBEDDING_DIMENSIONS` | `1536` | Corrected from a live probe if it differs |

### Azure AI Search

| Variable | Default | Effect |
|---|---|---|
| `RETRIEVER_BACKEND` | `local` | `local` or `azure` |
| `AZURE_SEARCH_ENDPOINT` | — | Service endpoint |
| `AZURE_SEARCH_API_KEY` | — | Admin key (needed to create the index) |
| `AZURE_SEARCH_INDEX` | `northwind-kb` | Index name |
| `AZURE_SEARCH_API_VERSION` | `2024-07-01` | REST API version |
| `AZURE_SEARCH_SEMANTIC` | `true` | Semantic ranker. Needs Basic SKU or above |

### TLS

| Variable | Default | Effect |
|---|---|---|
| `AZURE_CA_BUNDLE` | — | Path to a corporate root CA |
| `AZURE_USE_SYSTEM_CERTS` | `false` | Use the OS trust store |
| `AZURE_TLS_VERIFY` | `true` | `false` disables verification. **Development only** |

### Behaviour

| Variable | Default | Effect |
|---|---|---|
| `RAG_PROFILE` | `improved` | `improved` or `baseline` (the naive comparison system) |
| `RAG_SOURCE_DIR` | `KnwoledgeBaseDocuments` | Corpus location |
| `RAG_DATA_DIR` | `data` | Index, manifest and embedding cache |
| `RAG_RETRIEVE_TOP_K` | `20` | Candidates fetched before reranking |
| `RAG_CONTEXT_TOP_K` | `5` | Chunks placed in the prompt. Lower ⇒ cheaper, worse multi-hop |
| `RAG_MIN_RELEVANCE` | `4.0` | Rerank score below which the system abstains. Raise ⇒ more refusals |
| `RAG_MAX_CONTEXT_CHARS` | `12000` | Context budget |
| `RAG_ENABLE_ANSWER_CACHE` | `true` | Single-turn answer cache, scoped per department |

### API and observability

| Variable | Default | Effect |
|---|---|---|
| `API_ALLOWED_ORIGINS` | `http://localhost:8000` | CORS allow-list, comma-separated |
| `API_ALLOW_ANONYMOUS` | `true` | **Set `false` in production** |
| `LOG_LEVEL` | `INFO` | Standard levels |
| `LOG_FORMAT` | `json` | `json` or `text` |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | — | Reserved; the exporter is a stub |

---

## 8. Operations

**When documents change.** Re-run `python scripts/ingest.py`. It is incremental:
unchanged documents are neither parsed nor embedded, modified documents have
their previous chunks purged before new ones land, and deleted documents are
removed. Re-running with no changes makes zero embedding calls. Details in
[docs/ingestion-flow.md](docs/ingestion-flow.md).

**Cache invalidation.** `POST /api/v1/ingest` clears the answer cache when
anything was written or purged. External ingestion (§6.4) does not — restart the
revision, or accept up to 256 stale cached answers.

**Observability.** Every request carries a correlation ID, returned in
`X-Correlation-Id` and present on every log line. Every answer carries a
per-stage trace:

```
condense · decompose · embed · search · version_filter · rerank
         · version_rank · context · generate · verify
```

With `LOG_FORMAT=json` these land in Log Analytics as structured fields. The
metrics worth alerting on: abstention rate (a jump means retrieval broke),
groundedness score, p95 latency by stage, and tokens per question by department.

**Cost control**, in descending order of effect: point
`AZURE_OPENAI_UTILITY_DEPLOYMENT` at a mini model; enable response caching at
API Management; lower `RAG_CONTEXT_TOP_K`; reduce embedding dimensions; set
per-department quotas at the gateway.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CERTIFICATE_VERIFY_FAILED` on every Azure call | TLS-inspecting proxy with a CA Python does not trust | §5.3 |
| `/health` returns 503, UI says "index is empty" | No index built, or `RAG_DATA_DIR` points elsewhere | `python scripts/ingest.py` |
| `DeploymentNotFound` | Deployment name ≠ model name; they are independent | `az cognitiveservices account deployment list` |
| Answers are extractive, not written | No reachable chat deployment | Check the startup log; it names the provider |
| Retrieval quality poor, log says `local-hashing` | No embedding deployment | Deploy `text-embedding-3-small` and re-ingest |
| `embedding dimension changed … will be dropped` | Embedding model changed under an existing index | `python scripts/ingest.py --force` |
| `Semantic search is not enabled for this service` | Free-tier Search | `AZURE_SEARCH_SEMANTIC=false`, or upgrade to Basic |
| Container unreachable through ingress | uvicorn bound to loopback | `--host 0.0.0.0` (already in the image `CMD`) |
| Replicas restart-looping | `/health` used as a liveness probe | Use TCP for liveness — §6.7 |
| Deleted documents still answered | Manifest lost between ingest runs | Persist `RAG_DATA_DIR` — §6.4 |
| Anyone can query any department | `API_ALLOW_ANONYMOUS` left at `true` | Set `false` |

---

## 10. What is not included

Stated plainly so nothing here is mistaken for more than it is.

- **The image is verified; the Azure deployment is not.** Both build variants
  were built and run, and the container was exercised with and without Azure
  credentials (§6.1). What has *never* run is everything from `az containerapp
  create` onward — no subscription was deployed to. Those commands follow the
  documented CLI surface but should be treated as a first draft, particularly
  the probe and secret-reference syntax, which Azure changes more often than the
  core create flags.
- **No IaC and no CI/CD.** §6 is `az` CLI steps. A real deployment wants Bicep
  (Azure Verified Modules) or Terraform for the resources, and a pipeline that
  builds, runs `verify_pipeline.py` and `verify_lifecycle.py`, gates on the
  evaluation's hallucination rate, and deploys a revision.
- **Authentication is a development stub.**
  [`src/rag/api/auth.py`](src/rag/api/auth.py) maps static bearer tokens to
  department scopes. The *authorization* half — claims driving a pre-filter
  inside the search query — is production-shaped; the *authentication* half must
  be replaced with Entra ID JWT validation.
- **Azure AI Search is contract-verified, not integration-verified.** §5.2 has
  the checklist to run once a real service exists.
- **Application Insights export is a stub.** The spans and correlation IDs
  exist; the exporter does not.
- **No autoscaling proof.** The concurrency rule in §6.7 is reasoned from where
  request time goes, not load-tested.
