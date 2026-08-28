# Scale review — 5M documents, 1M queries/month

A review of whether this design carries to millions of documents with optimised
token cost, without degrading the query experience — and where the gaps are.

The numbers below are **measured against this corpus**, not estimated. Where a
figure is a projection it says so, and says what it was projected from.

- [What was measured](#what-was-measured)
- [Projection to target scale](#projection-to-target-scale)
- [Defects found and fixed](#defects-found-and-fixed)
- [The async conversion](#the-async-conversion)
- [Gap register](#gap-register)
- [What holds without change](#what-holds-without-change)
- [Correcting an earlier claim](#correcting-an-earlier-claim)

---

## What was measured

| | Measured | Source |
|---|---|---|
| Documents | 11 | `python scripts/ingest.py` |
| Chunks | 127 → **11.5 per document** | ingest report |
| Embedded text | 357 chars ≈ **89 tokens** per chunk | mean over the 127 chunks |
| Tokens per query | 2,588 prompt + 277 completion | trace, `improved` profile |
| LLM calls per query | **3** (rerank, generate, verify) | trace |
| Cost per query | **$0.0092** | gpt-4o rates, every stage |
| End-to-end latency | ~5s | trace, Azure OpenAI enabled |

The chunks-per-document ratio is the load-bearing one, and it is
corpus-dependent. This corpus is short policy documents; a corpus of long
reports or manuals would be several times denser. Read the projections below as
the low end of a range.

## Projection to target scale

**5M documents · 1M queries per month:**

| | Projection |
|---|---|
| Chunks | ~57.7M |
| Embedding, one-off | 5.2B tokens ≈ **$103** |
| Raw float32 vectors | **355 GB** (→ ~89 GB with int8, ~30 GB with int8 at 512 dims) |
| Query-time tokens | **$9,240 / month** |

**The ratio is the finding: ~90:1 between one month of query cost and the entire
one-off embedding bill.** Ingest-side token optimisation — smaller chunks,
cheaper embedding models, deduplication before embedding — is close to
worthless here. Every lever that matters is on the query path or in storage.

That reorders the usual instinct. The three highest-value cost levers, in order,
are a **shared answer cache**, **moving utility LLM calls off `gpt-4o`**, and
**sampling verification** — gaps 1, 2 and 3 below. Together they are worth
several times more than anything available on the ingestion side.

---

## Defects found and fixed

These were implementation defects, not architectural ones. All four are fixed in
this repository and each was verified by measurement rather than asserted.

### 1. Severe — the API served one request at a time

`chat`, `health`, `documents` and `ingest` in
[api/app.py](../src/rag/api/app.py) were `async def`, so FastAPI ran them **on
the event loop**. Everything inside was synchronous, including the `httpx.Client`
in [providers/http.py](../src/rag/providers/http.py) — there was no `await`
anywhere in the request path.

A replica therefore served one request at a time while spending ~95% of a ~5s
request blocked on network I/O: roughly **0.2 req/s per replica**. The
autoscaling design ("scale on concurrency, max 10 replicas") silently assumed a
replica handles many concurrent requests. At 1M queries/month it would not have.

This was fixed in two steps, and the intermediate one is worth keeping on the
record because it is where most codebases stop.

**Step 1 — plain `def` handlers.** FastAPI runs a sync handler in its anyio
threadpool, which is the right home for blocking I/O. Measured, 4 identical
concurrent requests, cache disabled: **21.5s → 5.53s** — 4.56× serialised
becoming 0.95× concurrent.

**Step 2 — async all the way down.** `httpx.AsyncClient`, awaited from handler
to transport. The threadpool fixed throughput but left the readiness probe
queueing behind saturated workers; see
[the async conversion](#the-async-conversion) for the numbers.

Correlation IDs were re-checked after each step: a supplied id round-trips in
the response header, the body and the trace.

### 2. Sub-query searches ran sequentially

[retrieve/pipeline.py](../src/rag/retrieve/pipeline.py) looped over decomposed
sub-queries issuing one blocking search each — up to 3 serial network round
trips on a multi-hop question. Now fanned out with `asyncio.gather`.

**Measured** end to end against a 120ms-per-search backend, one multi-hop
question: **284ms → 156ms**. That single-request improvement is entirely this
change — it is the same work, overlapped.

This needed [store/local.py](../src/rag/store/local.py) to become thread-safe —
`_rebuild()` now runs under a lock with double-checked locking. `httpx.Client`
was already thread-safe.

### 3. `/health` ran an index-wide aggregation on every probe

`health()` called `stats()`, which on Azure issued a `$count` **plus a facet
query across the whole index**. Container Apps polls readiness every ~10s per
replica, so this was a recurring aggregate on the probe path — at 57M chunks,
permanently.

**Fix:** `stats(*, full: bool = False)` on the backend protocol. The cheap path
does chunk count only; `sync()` and startup pass `full=True`. `health()` also
caches for 15s, since probe cost multiplies by replica count and never amortises.

**Measured** against the offline REST stub: 10 probe-style `stats()` calls now
issue **0** aggregation requests, down from 10.

### 4. Document count was silently wrong past 1,000 documents

`_facets()` requested `doc_id,count:1000`, so `stats().documents` reported at
most 1000 however many existed — wrong from 1,001 documents upward, with no
signal at all. At target scale it would have under-reported by ~5,000×.

**Fix:** the cap is now a named `FACET_LIMIT`, the cheap path does not compute
the value at all, and the full path sets `documents_exact=False` and logs a
warning when the cap is hit — reporting a lower bound honestly instead of a
confident wrong number.

**Measured:** with `FACET_LIMIT` lowered to 3 against 5 documents,
`documents_exact` correctly reports `False`.

---

## The async conversion

Defect 1's threadpool fix was correct but not final. This is the end state:
`httpx.AsyncClient` awaited from the handler down to the transport, with every
caller — API, CLI, ingest, evaluation, the verify scripts — converted rather
than wrapped in a sync facade.

### What "non-blocking" actually required

`async def` does not make anything non-blocking. A mechanical keyword sweep
would have been **worse** than the threadpool, because work that the threadpool
was safely isolating in a worker would have moved onto the event loop. Three
places do real work with no network in them, and each is dispatched with
`asyncio.to_thread` rather than relabelled:

| Work | Where |
|---|---|
| BM25 + cosine over every chunk | `LocalHybridStore._search_sync` |
| Signed feature hashing | `LocalEmbedder._embed_sync` |
| PDF/DOCX/XLSX parsing, corpus hashing, index writes | `sync()`, `Manifest.scan`, `_save_sync` |

Constructors cannot `await`, and three did network I/O. `AssistantService.__init__`
is now pure; `await AssistantService.create(settings)` does the probing.

### Measured

Identical conditions throughout: the search backend is
[`_azure_search_stub.py`](../scripts/_azure_search_stub.py) **in its own
process** with a 120ms modelled round trip per call (in-process, its request
threads contend with the event loop for the GIL and contaminate the result), the
answer cache off, one multi-hop question that fans out to 3 sub-query searches.

| | Single | 4 concurrent | 60 concurrent | 150 concurrent |
|---|---|---|---|---|
| **1. `async def` over blocking calls** | 284ms | 1387ms (4.9×) | 17552ms (61.9×) | 42777ms (150.9×) |
| **2. `def` + threadpool** | 156ms | 225ms (1.4×) | 2032ms (13.0×) | 4847ms (31.0×) |
| **3. async all the way** | **156ms** | **221ms (1.4×)** | **1935ms (12.4×)** | **4071ms (26.1×)** |

State 1 is fully serialised — 150 requests cost 150.9× one request, exactly as
the defect predicted. State 2 is the threadpool fix. State 3 is this change.

**On throughput, state 3 beats state 2 modestly**: 5% at 60 concurrent, 16% at
150. Both are ultimately bounded by the backend's own latency, so this is the
honest size of the win and it is not the reason to do the work.

**The reason to do the work is the readiness probe.** `/health` latency measured
during the same bursts:

| | p50 @ 60 conc | p95 @ 150 conc | max @ 150 conc |
|---|---|---|---|
| 2. threadpool | 780ms | 3373ms | 3373ms |
| 3. async | **7.3ms** | **317ms** | **1101ms** |

Under the threadpool, a trivial probe queued behind saturated workers: **107×
slower at p50**. A Container Apps readiness probe with a 1s timeout would fail,
and the orchestrator would kill a replica *because it was successfully serving
traffic* — the shape of a cascading failure. Async removes that inversion.

*(The sample counts tell the same story: the poller managed n=3 probes during
the threadpool's 60-request burst and n=55 during async's.)*

### Pool sizing is load-bearing, and I got it wrong first

The first async run measured `/health` p50 at **297ms — worse than the
threadpool**. The cause was mine: I had set `max_connections=64`, and 60
concurrent questions × 3 fan-out searches want ~180. The probe's own `$count`
was queueing for a connection.

The pool is now `AZURE_HTTP_MAX_CONNECTIONS`, default 200. Re-measured: p50
**7.3ms**. Worth stating plainly — async makes the pool the binding constraint
rather than the thread count, so the pool has to be sized for the fan-out, not
for the request count.

### Concurrent embedding batches

`AzureOpenAIEmbedder.embed()` looped serially over batches of 16. At the
measured 11.5 chunks/document, 5M documents is ~3.6M batches laid end to end.
Now `asyncio.gather`, bounded by an `asyncio.Semaphore`
(`AZURE_EMBED_CONCURRENCY`, default 8) — the back-pressure valve against TPM
quota, since unbounded fan-out over millions of batches does not go faster, it
just converts the job into 429s.

**Measured** against an embeddings endpoint with 200ms modelled latency,
256 texts = 16 batches:

| | |
|---|---|
| Serial | ~3200ms |
| Actual | **451ms** |
| Speedup | **7.1×** |
| Peak concurrent in flight | 8 — the semaphore holds exactly |
| Vector order preserved | yes (`gather` returns in argument order) |

### What did not change

Correlation ids round-trip through header, body and trace; all 10 pipeline
stages are still recorded in order, with no interleaving — the sub-query
fan-out deliberately opens no `stage()` inside its tasks. Security trimming
still holds (`demo-hr` sees 2 documents, `demo-admin` 11). `verify_pipeline`
9/9 and 53 checks, `verify_lifecycle` 23/23, `verify_docs` 0 failed, and the
multi-hop evaluation is unchanged at 25% correct / hit@5 1.0.

---

## Gap register

Not fixed here. Ranked by what actually moves the needle at target scale.

| # | Gap | Impact at 5M docs / 1M queries |
|---|---|---|
| 1 | **Answer cache is per-replica, in-memory, 256 entries** ([service.py](../src/rag/service.py)) | Hit rate divides by replica count and resets on every deploy. A shared Redis cache is the single largest cost lever — enterprise question distributions are heavily repetitive; even 30% hits saves ~$2,800/month |
| 2 | **Utility calls run on `gpt-4o`** | Rerank and verify are ~2 of the 3 calls per query. On the Azure backend rerank becomes free (semantic ranker does it in-service); verify on a mini deployment cuts most of the rest. Together roughly **3–4× on query cost** |
| 3 | **Verification runs on 100% of answers** | Doubles context tokens. Gate it on confidence and on answers containing figures, or sample it — most of the value at a fraction of the tokens |
| 4 | **No prompt caching** | The system prompt is re-sent on every call. Azure OpenAI caches repeated prefixes; this is free money |
| 5 | **No streaming** | Time-to-first-token equals total latency (~5s measured). The largest *perceived* latency win available, it changes no cost, and the async conversion above is its prerequisite |
| 6 | **`Manifest.scan()` hashes every file every run** | At 5M documents this reads the entire corpus to detect changes. Needs event-driven ingestion (already the designed target) or an mtime+size pre-filter before hashing |
| 7 | **Manifest and embedding cache are single JSON files loaded whole** | Both `json.loads` an entire file into memory. Unworkable at millions of entries; moves to Cosmos DB partitioned by `doc_id` |
| 8 | **`reconcile_versions()` is O(N) over the whole corpus per run** | Correct at 11 documents, wasteful at 5M. Needs scoping to the affected version *series* rather than the full corpus |
| 9 | **No query routing** | Every question searches everything. A cheap department/date pre-filter turns one huge search into one small one — cheaper *and* less noisy |
| 10 | **Vector storage uncompressed** | 355 GB raw. Scalar quantization ≈ 4× reduction, 512 dims ≈ 3× more, and they compose |

Gaps 1–5 are query-path work and are where the money is. Gaps 6–8 are ingestion
scaling and are the ones that would *break* rather than merely cost, so they
gate the migration; the architecture already names event-driven ingestion and
Cosmos DB as their destination.

---

## What holds without change

Worth stating explicitly, since the list above is all problems:

- **The `SearchBackend` seam.** Moving to Azure AI Search changes no pipeline
  code, and it is what makes gap 2's largest component (free reranking) a
  configuration change rather than a rewrite.
- **Security filters are applied inside the query**, not after it. This is the
  one that would be genuinely expensive to retrofit, and it is already right —
  post-filtering would both leak document existence and let another
  department's chunks consume top-k slots.
- **Deterministic chunk IDs and content-hash change detection.** Re-ingestion is
  idempotent, and unchanged documents are never re-embedded. At 5M documents the
  *scan* needs replacing (gap 6), but the incremental model behind it is right.
- **Version reconciliation patches rather than re-embeds.** A superseded
  document has its `is_current` flag patched without re-parsing or re-embedding
  a single chunk.
- **Cost shape.** Embedding at 90:1 against query cost means the ingestion path
  can stay comparatively unoptimised without it mattering.

---

## Correcting an earlier claim

An earlier draft of [architecture.md](architecture.md) asserted "5–10 million
documents (~500M chunks)" and "~3 TB of raw float32". That figure was never
anchored to anything measured.

Measured here, 5M documents is **~58M chunks and ~355 GB** — roughly an order of
magnitude out. `architecture.md` now states the measured ratio, flags it as
corpus-dependent, and gives a range instead of a single confident wrong number.
