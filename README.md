# Enterprise Knowledge Assistant — Azure RAG

A production-shaped RAG assistant over an enterprise document set (HR, Finance,
IT, Legal, Sales), built on the Azure AI stack, with a working answer to each of
the six RAG failure scenarios and a measured before/after evaluation.

The emphasis is on the parts that decide whether a RAG system is trustworthy:
what happens when the answer is in a table, when two documents disagree, when
the answer isn't in the corpus at all, and when the user asks a follow-up.

```
Documents → Parsing → Chunking → Embeddings → Azure AI Search *
          → Retrieval / Reranking → Context → LLM → Grounded Answer + Citations
```

<sub>* The search stage sits behind a `SearchBackend` protocol with two
implementations. `AzureAISearchStore` is the production one; a pure-Python
hybrid store is the **default**, so the repo runs with no cloud dependency.
Flip with `RETRIEVER_BACKEND=azure`. Both are verified —
see [docs/pipeline.md](docs/pipeline.md).</sub>

Every stage is mapped to its code and re-checked by a runnable verifier:

```bash
python scripts/verify_pipeline.py     # 9/9 stages, 52-53 checks
```

| Deliverable | Where |
|---|---|
| **Deployment — local, Azure, production** | [Deployment.md](Deployment.md) |
| Pipeline coverage — all nine stages | [docs/pipeline.md](docs/pipeline.md) |
| Production architecture (Step 2) | [docs/architecture.md](docs/architecture.md) · [PDF](docs/architecture.pdf) |
| Document lifecycle: add / modify / delete | [docs/ingestion-flow.md](docs/ingestion-flow.md) |
| The six failure scenarios (Step 3) | [docs/failure-scenarios.md](docs/failure-scenarios.md) |
| Evaluation, baseline vs improved (Step 4) | [docs/evaluation.md](docs/evaluation.md) · [eval/results/comparison.md](eval/results/comparison.md) |
| Architecture & problem-solving answers (Step 5) | [below](#step-5--architecture--problem-solving-answers) |

---

## Quick start

Python 3.14 (pinned in `.python-version`; 3.11+ supported) and seven packages — `fastapi`, `uvicorn`, `pydantic`, `httpx`,
`python-dotenv`, `PyMuPDF`, `python-docx`. No `azure-*` dependency: every Azure
call goes over REST (see [why](#why-rest-instead-of-the-sdks)).

```powershell
uv venv                            # honours .python-version (3.14)
.\.venv\Scripts\Activate.ps1      # bash: source .venv/bin/activate
uv pip install -r requirements.txt
```

Without `uv`, name the interpreter explicitly — `py -3.14 -m venv .venv` on
Windows, `python3.14 -m venv .venv` elsewhere. A bare `python -m venv` picks
whatever is on `PATH`, which is how you end up with a venv whose interpreter
and compiled packages disagree.

Full setup, including the Azure paths and production deployment, is in
**[Deployment.md](Deployment.md)**.

```bash
# 1. Build the index (no Azure needed — falls back to a local embedder)
python scripts/ingest.py

# 2. Run it
python -m uvicorn rag.api.app:app --app-dir src --port 8000
#    → http://localhost:8000

# or from the terminal
python scripts/cli.py ask "What is the nightly hotel cap in London?"
python scripts/cli.py chat --department Sales
python scripts/cli.py compare "What was the Professional tier price in 2025?"

# or in a container (342 MB, Python 3.14, index baked in, non-root)
docker build -t rag-assistant . && docker run --rm -p 8000:8000 rag-assistant
```

Connecting Azure, local testing, and deploying to Azure Container Apps are all
covered step by step in **[Deployment.md](Deployment.md)**.

Azure OpenAI requires an explicit `AZURE_OPENAI_ENABLED=true`: credentials
sitting in your environment are deliberately not enough to make a "local" run
call a cloud model. Startup logs state exactly which providers are live, so a
demo can never silently run on a weaker — or costlier — stack than you think:

```
embedding provider active  provider=local-hashing dimensions=768
chat provider active       provider=azure-openai:vsquare-gpt-4o
search backend active      backend=local
assistant ready            profile=improved chunks=127 documents=11
```

### Connecting Azure

Copy `.env.example` to `.env` and fill in what you have. Everything is optional
and degrades explicitly.

```bash
AZURE_OPENAI_ENABLED=true          # master switch — nothing calls Azure without it
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com
AZURE_OPENAI_API_KEY=<key>
AZURE_OPENAI_CHAT_DEPLOYMENT=gpt-4o
AZURE_OPENAI_UTILITY_DEPLOYMENT=gpt-4o-mini        # rerank / condense / verify / judge
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-3-small
```

To use Azure AI Search instead of the local index:

```bash
bash scripts/provision_azure_search.sh            # creates the service (optional)
export RETRIEVER_BACKEND=azure
export AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
export AZURE_SEARCH_API_KEY=<admin-key>
python scripts/ingest.py --force                  # creates the index and loads it
```

> **Behind a TLS-inspecting corporate proxy?** Azure calls will fail with
> `CERTIFICATE_VERIFY_FAILED`. Point `AZURE_CA_BUNDLE` at your corporate root
> CA, or set `AZURE_USE_SYSTEM_CERTS=true`. `AZURE_TLS_VERIFY=false` exists as a
> last-resort local-development escape hatch and logs a warning every start.

---

## Technology stack

| Service / library | Why it is here |
|---|---|
| **Azure OpenAI** — `gpt-4o` | Grounded answer synthesis, and the reranking / query-condensation / verification passes |
| **Azure AI Search** | Hybrid (BM25 + vector) retrieval, semantic reranking, and filterable metadata for security trimming — all in one query |
| **Azure Blob Storage** | Source of truth for documents; `BlobCreated`/`BlobDeleted` events drive incremental ingestion |
| **Azure Functions** (design) | Event-driven parse → chunk → embed → upsert, decoupled from serving |
| **Azure AI Document Intelligence** (design) | Layout model for table structure in scanned or complex PDFs |
| **Azure Container Apps / App Service** | Stateless API tier, autoscaled on concurrency |
| **API Management** | Rate limiting, quotas, response caching, per-caller keys |
| **Microsoft Entra ID** | Authentication; group claims map to department scopes |
| **Key Vault + Managed Identity** | No secrets in code or configuration |
| **Application Insights** | Per-stage traces, correlation IDs, cost and quality metrics |
| **FastAPI + uvicorn** | The API and the single-page chat UI |
| **PyMuPDF / python-docx** | Table-structure-preserving parsing — the fix for Scenario 1 |

---

## How it works

### Retrieval pipeline

```
query + conversation history
  → condense into a standalone question         Scenario 6
  → decompose into sub-queries if multi-hop     Scenario 2
  → security filter, applied inside the query   Step 5 Q4
  → hybrid retrieve per sub-query, fuse by RRF  Scenario 1
  → rerank the top 20                           Scenario 1, Step 5 Q1
  → version-aware re-ranking                    Scenario 3
  → select context, balanced across sub-queries Scenario 2
  → sufficiency / ambiguity gates               Scenarios 4, 5
  → grounded generation with numbered citations
  → post-hoc verification → confidence          Step 5 Q6
```

Two profiles share this code path. `RAG_PROFILE=baseline` is a genuinely naive
implementation — fixed 512-character chunks over a naive text dump, single
top-5 vector search, no reranking, no metadata, no guardrails — kept
permanently so the evaluation has a real "before" rather than a strawman.

### Two pluggable seams

```
SearchBackend (Protocol)              EmbeddingProvider (Protocol)
├── LocalHybridStore   [default]      ├── AzureOpenAIEmbedder  [probed at startup]
└── AzureAISearchStore [env flip]     └── LocalEmbedder        [fallback]
```

The local hybrid store implements BM25 + cosine + RRF + in-query filtering in
pure Python, so the whole pipeline — including the evaluation — is runnable and
reproducible with no cloud dependency. `AzureAISearchStore` implements the same
contract against the Search REST API, including the index definition (HNSW
vector profile, semantic configuration, filterable metadata) and native hybrid
queries. Nothing in the pipeline branches on which is active.

The local embedder is a genuine fallback, not a pretend one, and its limits are
stated plainly: it captures lexical and morphological overlap (word unigrams,
bigrams, character 4-grams) but has **no notion of synonymy**, so "time off"
will not match "PTO" the way a real embedding does. That is exactly why the
retriever is hybrid and why the startup log names the active provider.

### Repository layout

```
src/rag/
  ingest/     parsers.py  metadata.py  chunker.py  manifest.py  sync.py
  providers/  embeddings.py  llm.py  http.py
  store/      base.py  local.py  azure_search.py  factory.py
  retrieve/   condense.py  decompose.py  rerank.py  recency.py  pipeline.py
  generate/   prompts.py  guardrails.py  answer.py
  api/        app.py  auth.py  schemas.py  static/chat.html
  observability/tracing.py
  text.py  models.py  config.py  service.py  cli.py
eval/       dataset.jsonl  metrics.py  run_eval.py  results/
Dockerfile  .dockerignore  Deployment.md
pyproject.toml
scripts/    cli.py  ingest.py  verify_pipeline.py  verify_lifecycle.py
            verify_docs.py
            _azure_search_stub.py  provision_azure_search.sh  render_pdf.py
docs/       pipeline.md  architecture.md  ingestion-flow.md
            failure-scenarios.md  evaluation.md
```

---

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/chat` | Ask a question. Returns answer, status, confidence, citations, retrieved passages with every score, and the full pipeline trace |
| `GET /health` | Readiness. Returns **503** when the index is empty — a probe that passes because the process is up would let an instance answer every question with "I don't know" |
| `GET /api/v1/documents` | What is indexed, with version status, trimmed to the caller |
| `POST /api/v1/ingest` | Incremental re-sync (admin only) |
| `GET /docs` | OpenAPI |

```bash
curl -s localhost:8000/api/v1/chat \
  -H 'Authorization: Bearer demo-sales' -H 'Content-Type: application/json' \
  -d '{"question":"How many weeks of paid parental leave do I get?"}'
```

Demo tokens `demo-admin`, `demo-hr`, `demo-finance`, `demo-it`, `demo-legal`,
`demo-sales` map to department scopes. A `demo-sales` caller asking the question
above gets nothing, because HR chunks never enter their candidate set — see
[Q4](#4-security).

---

## Evaluation

35 questions across 11 categories — straightforward, table lookups,
wrong-chunk traps, multi-hop, versioning, ambiguous, unanswerable, follow-ups
and access control — plus deliberate **control cases** that must *not* trigger
a clarification or an abstention.

| | Baseline | Improved | |
|---|---:|---:|---|
| **Answer correctness** | 63% | **97%** | ▲ 34 pts |
| **Hallucination rate** | 11% | **3%** | ▼ 9 pts |
| **Abstention accuracy** | 38% | **88%** | ▲ 50 pts |
| hit@1 / hit@5 | 78% / 93% | **100% / 100%** | ▲ 22 / ▲ 7 pts |
| Section hit rate | 0% | **85%** | ▲ 85 pts |
| Citation correctness | 89% | **100%** | ▲ 11 pts |
| Groundedness | 93% | **99%** | ▲ 6 pts |
| LLM judge score | 64% | **86%** | ▲ 21 pts |
| Latency p50 | 3.3 s | 5.9 s | ▲ 2.6 s |
| Cost per question | $0.0051 | $0.0098 | ▲ $0.0047 |

**15 cases fixed, 0 regressions.** Categories that went from 0% to 100%:
access control, ambiguity, follow-ups. The latency and cost regressions are
real and come from the same source — three extra model calls — and most of
both is recoverable by pointing the utility calls at a mini deployment.

```bash
python scripts/ingest.py --profile baseline
python scripts/ingest.py --profile improved
python eval/run_eval.py --profile baseline --out eval/results/baseline.json
python eval/run_eval.py --profile improved --out eval/results/improved.json
python eval/run_eval.py --compare eval/results/baseline.json \
                                  eval/results/improved.json \
                                  --report eval/results/comparison.md
```

Full results and per-metric attribution: [docs/evaluation.md](docs/evaluation.md).

Two scoring rules keep the numbers honest:

- **`forbidden_facts`** — a versioning question is only correct if the answer
  says `$65` *and does not say* `$59`. Without the negative check, an answer
  that hedges by quoting both prices scores as correct.
- **`hallucination`** — answering at all is a failure when the corpus has no
  answer, regardless of how good the prose is.

---

## Step 5 — Architecture & problem-solving answers

### 1. Retrieval quality

> *Your chatbot retrieves 5 chunks, but only one is relevant. How would you debug
> and improve it?*

**First, separate the two failures that look identical from the outside.** Was
the right chunk not in the index, not retrieved, or retrieved but ranked below
four distractors? Those need completely different fixes, and averaging them
together is how teams spend a week tuning chunk size on a parsing bug. The
response trace in this system reports each stage's output for exactly this
reason.

The diagnostic ladder I actually used on this corpus:

1. **Is the answer in the index at all, in a usable form?** Grep the chunk text
   for the expected fact. On this corpus that step is where the real bug was:
   PDF tables were being flattened column-by-column and DOCX tables detached from
   their headings, so `$350` existed but `Tier 1 | … London | $350` did not. No
   amount of chunk-size or Top-K tuning recovers information destroyed at parse
   time.
2. **Is it retrievable lexically and semantically?** Run the query in
   keyword-only and vector-only mode (`mode=keyword` / `mode=vector` are first
   class in the backend). If keyword-only fails on a term that is literally in
   the chunk, the analyzer is wrong — that is how I found that "approve" was not
   matching "Approver"/"approval" for lack of stemming.
3. **Is it a ranking problem?** Look at the candidates at k=20, not k=5. If the
   right chunk sits at rank 12, the retriever is fine and the ranker is the
   problem. Fix with a reranker, not with a bigger k.
4. **Is the chunk interpretable alone?** A chunk is retrieved without its
   neighbours. `Tier 1 | New York, San Francisco, Boston, London | $350` contains
   no word suggesting "hotel" or "nightly cap". Prefixing each chunk with a
   `Title > Section` breadcrumb is the single highest-leverage change here.

**The improvements, in the order I would apply them** (roughly descending value
per unit of effort): structure-preserving parsing; heading breadcrumbs on every
chunk; hybrid retrieval so exact tokens survive; a reranker over the top 20;
metadata filters to shrink the candidate pool; and only then chunk-size tuning,
which is the knob most often turned first and rewards it least.

### 2. Latency

> *Production response time increases from 3 seconds to 12 seconds. How would you
> identify the bottleneck?*

Every request already carries a correlation ID and a per-stage timing breakdown,
so the first move is to read it rather than to speculate:

```
condense=0.0ms decompose=0.0ms embed=1.2ms search=20.3ms
version_filter=0.2ms rerank=1835.3ms version_rank=0.0ms
context=0.1ms generate=1457.0ms verify=1621.9ms   total=4936.0ms
```

That instantly rules out three quarters of the pipeline. Compare the p50 and p95
stage breakdown before and after the regression and the culprit names itself.
The realistic candidates, roughly in order of how often they are the answer:

| Symptom in the trace | Likely cause | Fix |
|---|---|---|
| `generate` up, tokens up | Context grew — more chunks, bigger chunks, or a longer conversation | Cap `context_top_k`, cap history, trim chunk size |
| `generate` up, tokens flat | Azure OpenAI throttling (429 + retry backoff) or a noisy-neighbour PTU | Check `retry-after` counts in logs; raise quota or add PTU |
| `rerank`/`verify` dominant | Utility calls routed to the large model | Route them to `gpt-4o-mini` — this is the case on the test resource here, and it accounts for ~3.6s of the 4.6s |
| `search` up | Index grew past its partition, replicas saturated, or a filter became non-selective | Add partitions/replicas; check filter selectivity |
| Everything up uniformly | Cold starts, or the app tier is CPU-throttled | Min instances > 0; check container CPU |
| Wall clock up, stages flat | Time is outside the pipeline — gateway, TLS, network, queueing | Look at APIM and Front Door metrics |

Two structural safeguards matter more than any single fix: **streaming the
answer** so time-to-first-token stays flat even when total time grows, and a
**response cache** at the gateway for repeated questions.

### 3. Scale

> *From 10,000 documents to 5 million. What changes?*

Covered in full in [docs/architecture.md](docs/architecture.md#scale). The
summary:

- **Partition and shard the index** along the same boundary as the security
  model (department / business unit / tenant), so each query touches one index
  and filters stay cheap.
- **Compress the vectors** — scalar or binary quantization, plus reduced
  dimensions (`text-embedding-3-small` at 512 rather than 1536). At 500M chunks
  the raw float32 storage is ~3 TB; this is the difference between feasible and
  not.
- **Two-stage retrieval** — cheap compressed ANN for recall, full-precision
  rescoring on the top few hundred.
- **Ingestion becomes a pipeline product** — durable orchestration, TPM-aware
  rate limiting, checkpointing, and a separate backfill path from the
  incremental one.
- **Add a routing stage** — a cheap classifier that narrows to a department or
  date range before retrieval, turning one huge search into one small one.

**What does not change:** hybrid + rerank, the guardrail chain, the security
pre-filter, and the ingestion contract. Those decide answer quality, and they
should not be re-litigated at every order of magnitude.

### 4. Security

> *HR documents must never be retrieved for Engineering users. How would you
> architect access-controlled RAG?*

**The filter must be inside the query, never after it.** This is the whole
answer, and it is the thing that is easy to get wrong.

Post-filtering — retrieve top-k, then drop what the user cannot see — fails in
two ways. Functionally, an HR chunk consumes one of the five slots the Sales
user's answer needed, so their results silently get worse the more restricted
content exists. And it leaks: the shape of the response, the latency, and the
count of dropped results all carry information about documents the user is not
supposed to know exist.

The layered design:

1. **Identity** — Entra ID issues the token; the API validates it and reads
   group/app-role claims.
2. **Claims → scope** — groups map to `Principal.departments`. In this repo
   that mapping is a dev token table
   ([`api/auth.py`](src/rag/api/auth.py)); in production it is a group lookup.
   Everything downstream is identical.
3. **Pre-filter in the query** — `SearchFilters(departments=[...])` becomes an
   OData filter evaluated by Azure AI Search *before* scoring:
   `search.in(department, 'Sales', ',')`.
4. **Physical isolation where the risk warrants it** — one index (or one search
   service) per department or per tenant. Then cross-department retrieval is
   impossible by construction rather than by correct filter code. This is what
   I would do for genuinely sensitive corpora — legal hold, HR investigations,
   M&A.
5. **Deny cleanly** — when nothing in scope matches, the assistant says so
   without hinting at what exists elsewhere.
6. **Audit** — every answer's trace records the exact chunks used and the
   caller, so any answer can be reconstructed and any access reviewed.

Two details that are easy to miss: **the cache must be scoped by principal**
(this one keys on department scope, and refuses to cache multi-turn answers at
all), and **document-level ACLs must be re-checked at query time** rather than
frozen at ingest, because group membership changes after indexing.

```
demo-sales:  "How many weeks of paid parental leave do I get?"
             → "I could not find anything on that in the documents available
                to you (Sales)."
demo-hr:     same question
             → "12 weeks of paid parental leave… [1]"  cites HR/LeavePolicy.pdf
```

### 5. Cost

> *Azure OpenAI costs suddenly increase. How would you identify the cause and
> optimize?*

**Identify first — the answer is almost always one of four things**, and the
per-request trace already carries prompt/completion tokens, model calls and
cache status, so this is a query, not an investigation:

1. **Volume** — more questions, or one caller looping. Group tokens by
   department and by caller; a runaway integration shows up immediately.
2. **Tokens per question** — context grew. This is the most common cause and the
   least noticed: someone raised `context_top_k`, or chunk size, or added a
   verification pass. Compare mean prompt tokens over time.
3. **Model mix** — utility calls (rerank, condense, verify, judge) drifted onto
   the expensive model. On this deployment that is exactly the situation: no
   `gpt-4o-mini` exists on the resource, so all four stages run on `gpt-4o`.
4. **Cache hit rate collapsed** — often after an ingest invalidated it, or a
   change to how the cache key is built.

**Optimizations, in descending order of value per unit of risk:**

| Lever | Typical saving | Cost |
|---|---|---|
| Route rerank/condense/verify to `gpt-4o-mini` | ~10× on ~75% of the calls | Negligible quality change on these tasks |
| Response cache for repeated questions (APIM or Redis) | Proportional to repeat rate | Must be scoped per principal |
| Embedding cache keyed by content hash | Near-100% on re-ingest | None — already implemented |
| Tighten `context_top_k` 5 → 3 | ~30% of prompt tokens | Hurts multi-hop questions; check the eval |
| Reduce embedding dimensions 1536 → 512 | 3× on embedding storage and query cost | Small retrieval quality cost |
| Per-department token quotas at APIM | Bounds worst case | Political, not technical |
| Prompt caching / PTU | Large at steady high volume | Commitment |

The one I would resist: shrinking the answer model. It is the stage where
quality is most visible and the token count is smallest.

### 6. Production failure

> *"Correct most of the time, but occasionally a completely wrong answer with a
> valid-looking citation." Explain your debugging approach.*

This is the failure mode I care most about, because it is invisible to the user
— a real citation to a real document makes a wrong answer indistinguishable
from a right one. The baseline in this repo reproduces it exactly:

```
Q: What is the cancellation policy for the Standard plan?
A: The cancellation policy for the Standard plan requires a 12-month contract
   term with auto-renewal. It can be canceled with 30 days' written notice
   prior to renewal. [2]
   [2] → Sales/Pricing2026.pdf     ← real document, real citation, invented tier
```

There is no Standard tier — the tiers are Starter, Professional, Enterprise and
Enterprise Plus. What makes this case instructive is that *nothing in the answer
is ungrounded*: the cited chunk really does say "Standard contract term is 12
months with auto-renewal, cancellable with 30 days' written notice". Every
figure checks out. The system answered accurately about a different entity.

**Walk the pipeline in order and ask one question at each stage.** Every answer
in this system carries the trace needed to do it retrospectively, which matters
because these reports arrive hours later.

| Stage | The question | What it looks like when this is the cause |
|---|---|---|
| **Query** | Was the rewritten query faithful? | `standalone_query` drifted from the user's intent — over-eager condensation on a question that did not need it |
| **Retrieval** | Was the right chunk in the candidate set? | Expected doc absent at k=20 → parsing or indexing problem, not generation |
| **Ranking** | Did a near-duplicate outrank the right chunk? | **The case above.** Two documents say almost the same thing; the wrong one wins on similarity because recency is not in the text |
| **Context** | What was actually in the window? | The trace lists the exact chunks. Often the answer is "the right chunk was retrieved and then dropped by the context budget" |
| **Prompt** | Did the model have what it needed to be careful? | No CURRENT/SUPERSEDED labelling, no instruction to quote figures exactly |
| **LLM** | Did it state something the sources do not? | Groundedness verifier flags unsupported figures |
| **Citation** | Does the cited chunk actually support the claim? | **The tell.** Citation resolves to a real document, but the claimed figure is not in that chunk's text |

The last row is why citations here point at **numbered chunks, not document
names**. "According to the Travel Policy" cannot be verified. `[2]` resolves to
a specific chunk whose exact text was in the window, so a claim can be checked
against it mechanically — which is what the numeric grounding check does on
every answer.

**The fixes this diagnosis leads to**, all implemented: corpus-wide
supersession metadata with superseded duplicates filtered out *before*
reranking (which is where the near-duplicate row of the table above bites);
CURRENT/SUPERSEDED labelling in the prompt with an explicit no-mixing rule;
post-generation verification; and withdrawal rather than caveating when
verification fails.

**And the limit of that diagnosis, stated honestly.** The Standard-plan case
above is the one my guardrails still do not reliably catch, because it is not a
grounding failure at all — it is an *entity* failure, and every grounding check
passes on it. A prompt rule ("check the sources actually define an entity by
that name") handles it most of the time; it is a prompt rule, not a
deterministic guardrail, and it fails intermittently. The robust fix is
entity-level verification: extract the named entity, require it to appear as a
defined term in the retrieved context, abstain otherwise. Not implemented —
see [what I would change](#what-i-would-change-before-production).

**And the systemic answer:** an offline eval set with `forbidden_facts`, run in
CI. A "wrong answer with a good citation" is only occasional in production —
it is 100% reproducible in an eval case. That is the difference between fixing
it and hoping.

---

## What I would change before production

Honest list, roughly in priority order:

1. **Deploy an embedding model.** The test resource has no embedding deployment,
   so retrieval currently runs on the local hashed-feature embedder, which has no
   semantic matching. `text-embedding-3-small` is the single biggest quality win
   available and takes minutes.
2. **Deploy a small model for the utility calls.** Rerank, condense and verify
   are ~75% of the model calls and ~78% of the latency. On `gpt-4o-mini` they
   cost roughly a tenth as much and run several times faster.
3. **Replace the dev token table with Entra ID JWT validation.** The
   authorization half is production-shaped; the authentication half is a stub.
4. **Move ingestion to the event-driven Function.** The synchronous
   `scripts/ingest.py` is right for a corpus of 11 documents and wrong for one
   that changes continuously.
5. **Stream the answer.** Time-to-first-token matters more than total time.
6. **Wire Application Insights properly** — the spans and correlation IDs exist,
   the exporter is a stub.
7. **Add entity-level verification.** The one remaining evaluation failure is
   an invented "Standard plan" that passes every grounding check because the
   cited sentence really does contain the word "standard". A prompt rule
   handles it most of the time; requiring named entities to appear as *defined*
   terms in the retrieved context would handle it deterministically.
8. **Persist evaluation results per commit** and gate merges on the
   hallucination rate and versioning categories.
9. **Add prompt-injection tests to the eval set.** Retrieved content is treated
   as data today, but that is asserted by prompt design, not by a test.

---

## Notes

### Why REST instead of the SDKs

Azure OpenAI and Azure AI Search are called over their REST APIs via `httpx`
rather than through `openai` / `azure-search-documents`. Two reasons: the repo
runs immediately on a machine that already has FastAPI, with no install step or
version-resolution risk; and the index definition and query shapes in
[`store/azure_search.py`](src/rag/store/azure_search.py) are exactly what the
Azure docs and portal show, which makes them easy to audit. Nothing depends on
this choice — the SDKs would slot into the same provider classes.

### Corpus

`KnwoledgeBaseDocuments/` — 11 fictional Northwind Traders documents across five
departments in PDF, DOCX and XLSX. `Pricing2025.pdf` and `Pricing2026.pdf`
deliberately conflict, which is what makes Scenario 3 demonstrable on real data.

### Regenerating the architecture PDF

```bash
python scripts/render_pdf.py docs/architecture.md
```

Works on any Markdown file in the repo. There is no network here, so no
`mermaid-cli`, no CDN and no pandoc — instead it converts Markdown to HTML with
a self-contained converter, renders the Mermaid diagrams to inline **SVG** using
the `mermaid.min.js` that ships inside Visual Studio, and prints via headless
Chrome. Diagrams stay vector (sharp at any zoom) and the text stays selectable.

It also *checks* legibility rather than assuming it: each diagram's rendered
label height is computed in points from its measured viewBox, the page
orientation is chosen per diagram from its own aspect ratio, and anything below
8pt is reported as a warning.

```
  #   size (px)      page       label    legible
  0     930x1433    inline       8.8pt  yes
  1     620x1414    inline      13.1pt  yes
```

That check is what drove splitting the production architecture into a query
path and an ingestion path: as one combined diagram it measured 2460×1250 px,
which is 5.1pt on an A4 landscape page — technically rendered, practically
unreadable.

### Verifying pipeline coverage

```bash
python scripts/verify_pipeline.py                  # local backend
python scripts/verify_pipeline.py --backend azure  # whole pipeline on the Azure adapter
```

52-53 checks across the nine stages (53 when `AZURE_OPENAI_ENABLED=true`), against a throwaway copy of the corpus.
Exits non-zero on failure, so it can gate CI. Full mapping in
[docs/pipeline.md](docs/pipeline.md).

Stage 5 runs against an offline stub of the Azure AI Search REST API
([`scripts/_azure_search_stub.py`](scripts/_azure_search_stub.py)) so it is
covered with no network and no Azure spend — proving the index definition,
hybrid + semantic query construction, OData security filters, delete-by-document
and metadata-merge-without-re-embedding. It is a **contract** test, not an
integration test; `docs/pipeline.md` carries the live checklist.

A few of these checks earn their keep by being hard to pass accidentally: the
parser check asserts the structured output *differs* from the naive dump, so a
regression to `page.get_text()` fails loudly rather than silently losing every
table; and the security-filter check first proves an unscoped query does reach
other departments, so a broken filter shows up as leaked results rather than as
zero results.

### Verifying the document lifecycle

```bash
python scripts/verify_lifecycle.py
```

23 assertions covering add / modify / delete, orphan-chunk prevention,
zero-cost re-ingestion, and reversible supersession. See
[docs/ingestion-flow.md](docs/ingestion-flow.md).
