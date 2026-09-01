# Learning Objectives 

An enterprise knowledge assistant: a retrieval-augmented generation (RAG) system
over a mixed corpus of policy documents, spreadsheets and contracts, built on
Azure AI Search and Azure OpenAI. It answers questions from retrieved passages,
cites the source of every claim, trims results to the asker's department, and
abstains rather than guessing when the corpus cannot support an answer. This
file tracks the **Deliverables** section against what is actually
in the repository — each item linked to its evidence, each marked honestly.

## Contents

- [Status key](#status-key)
- [1. GitHub Repository](#1-github-repository)
- [2. Architecture Diagram](#2-architecture-diagram)
- [3. Evaluation Results](#3-evaluation-results)
- [4. Demo + Architecture Presentation Video](#4-demo--architecture-presentation-video)
- [5. RAG Failure Scenarios](#5-rag-failure-scenarios)
- [6. Capabilities](#6-capabilities)
- [7. Debugging Process](#7-debugging-process)
- [8. Query Resolution & Evaluation Metrics](#8-query-resolution--evaluation-metrics)
- [9. Tracing a Live Query](#9-tracing-a-live-query)
- [Summary](#summary)

## Status key

| | Meaning |
|---|---|
| ✅ **Done** | Built, and verified in this repository |
| ⚠️ **Partial** | Built, with a stated limit on what has actually been proven |
| 🔲 **TO DO** | Not done |

Sub-item names below are the brief's own wording.

---

## 1. GitHub Repository

**✅ Done** — [github.com/vramanavar1/rag-knowledge-assistant](https://github.com/vramanavar1/rag-knowledge-assistant)

| Sub-item | Status | Evidence |
|---|---|---|
| Python RAG application | ✅ | [`src/rag/`](src/rag/) — HTTP API (HyperText Transfer Protocol Application Programming Interface) [`api/app.py`](src/rag/api/app.py), chat UI (user interface) [`api/static/chat.html`](src/rag/api/static/chat.html), CLI (command-line interface) [`cli.py`](src/rag/cli.py). Run it: [README § Quick start](README.md#quick-start) |
| ingestion pipeline | ✅ | [`src/rag/ingest/`](src/rag/ingest/) — parse, chunk, metadata, incremental sync. Entry point [`scripts/ingest.py`](scripts/ingest.py); lifecycle documented in [`docs/ingestion-flow.md`](docs/ingestion-flow.md) |
| retrieval pipeline | ✅ | [`src/rag/retrieve/`](src/rag/retrieve/) — condense, decompose, hybrid retrieve + RRF (Reciprocal Rank Fusion), rerank, version-rank. All nine assignment stages verified 9/9 (53 checks) in [`docs/pipeline.md`](docs/pipeline.md) |
| Azure AI Search integration | ⚠️ | [`src/rag/store/azure_search.py`](src/rag/store/azure_search.py) — complete REST (Representational State Transfer) client: index definition with HNSW (Hierarchical Navigable Small World) vector profile and semantic configuration, native hybrid query, OData (Open Data Protocol) security filters, delete-by-document, metadata merge. Passes 21/21 contract checks — but **against the offline stub** [`scripts/_azure_search_stub.py`](scripts/_azure_search_stub.py), **never against a live service**. [`docs/pipeline.md`](docs/pipeline.md) carries the live-integration checklist |
| Azure OpenAI integration | ⚠️ | [`src/rag/providers/llm.py`](src/rag/providers/llm.py) — chat completions, exercised against a real `gpt-4o` deployment. [`src/rag/providers/embeddings.py`](src/rag/providers/embeddings.py) is implemented and batched, but **every recorded evaluation run used the local fallback embedder**: no embedding deployment was available on the test resource, as stated in [`docs/evaluation.md`](docs/evaluation.md#headline) |
| evaluation scripts | ✅ | [`eval/run_eval.py`](eval/run_eval.py) (run, compare, rescore), [`eval/metrics.py`](eval/metrics.py), 35-question dataset [`eval/dataset.jsonl`](eval/dataset.jsonl) |
| README | ✅ | [`README.md`](README.md) |

---

## 2. Architecture Diagram

> *"Show the proposed production Azure architecture."*

**✅ Done** — [`docs/architecture.md`](docs/architecture.md#target-architecture)

Two Mermaid diagrams, drawn separately because they have different triggers and
failure modes: the **query path** (Front Door → API Management → Container Apps →
retrieval pipeline → Azure AI Search / Azure OpenAI) and the **ingestion path**
(Blob → Event Grid → Queue → Functions → parse, chunk, embed, upsert). Rendered
to [`docs/architecture.pdf`](docs/architecture.pdf) with both diagrams legible.

---

## 3. Evaluation Results

**✅ Done**

| Sub-item | Status | Evidence |
|---|---|---|
| Baseline vs Improved RAG | ✅ | [`eval/results/comparison.md`](eval/results/comparison.md) — generated. Method and interpretation in [`docs/evaluation.md`](docs/evaluation.md#headline): 35 questions, 11 categories. Correctness 63% → 97%, hallucination 11% → 3%, abstention accuracy 38% → 88%. **15 cases fixed, 0 regressions** |
| Factors/Changes impacting improved metrics | ✅ | [`docs/evaluation.md` § Where the improvement came from](docs/evaluation.md#where-the-improvement-came-from) — attributes each metric movement to a specific change. What the two profiles actually differ by is in [§ What is being compared](docs/evaluation.md#what-is-being-compared) |

---

## 4. Demo + Architecture Presentation Video

> *"Record a 5 minute video covering:"*

**🔲 TO DO** — no recording exists yet.

Every topic below is outstanding **as a recording**. The right-hand column links
the material already written, so the video can be scripted from the repository
rather than composed from scratch.

| Video topic | Status | Source material to draw on |
|---|---|---|
| Architecture | 🔲 TO DO | [`docs/architecture.md` § Target architecture](docs/architecture.md#target-architecture) |
| Azure services selected and why | 🔲 TO DO | [§ Why this architecture](docs/architecture.md#why-this-architecture) and [§ Why Azure AI Search](docs/architecture.md#why-azure-ai-search) |
| Working chatbot | 🔲 TO DO | [`src/rag/api/static/chat.html`](src/rag/api/static/chat.html); how to start it: [`Deployment.md`](Deployment.md) |
| One or two RAG failure examples | 🔲 TO DO | **[§ RAG Failure Scenarios](#5-rag-failure-scenarios) below** — four written up for this purpose. Full set: [`docs/failure-scenarios.md`](docs/failure-scenarios.md), each with a symptom. [Scenario 1](docs/failure-scenarios.md#scenario-1--correct-document-wrong-chunk) is the strongest demo: a table destroyed at parse time |
| How you diagnosed them | 🔲 TO DO | The **How it was diagnosed** part of each [scenario below](#5-rag-failure-scenarios); also the *Root cause* and *Evidence* subsections in the full write-ups, plus the diagnostic ladder in [README § Step 5](README.md#step-5--architecture--problem-solving-answers) |
| Improvements implemented | 🔲 TO DO | The **What was changed** part of each [scenario below](#5-rag-failure-scenarios); the *The fix* subsection of each full write-up; the five profile branches in [`docs/evaluation.md`](docs/evaluation.md#what-is-being-compared) |
| Evaluation before vs after | 🔲 TO DO | [`docs/evaluation.md` § Headline](docs/evaluation.md#headline), [`eval/results/comparison.md`](eval/results/comparison.md) |
| What you would change before production deployment | 🔲 TO DO | [`docs/scale-review.md` § Gap register](docs/scale-review.md#gap-register) — ten ranked gaps; and [`docs/architecture.md` § Scale](docs/architecture.md#scale) |

The brief also notes: *"The candidate should personally explain the architecture
and technical decisions."*

---

## 5. RAG Failure Scenarios

Four of the six scenarios from
[`docs/failure-scenarios.md`](docs/failure-scenarios.md), 
**what fails**, **how it was diagnosed**, **what was
changed**, and **how that specific scenario was evaluated**. That last part is
the one usually left out — a fix nobody measured is an opinion.

Every percentage below is re-derived from `eval/results/baseline.json` and
`eval/results/improved.json`, and `n=` is stated wherever a change is claimed.
Each scenario is run on its own with:

```bash
python eval/run_eval.py --profile baseline --category <name> --no-judge
python eval/run_eval.py --profile improved --category <name> --no-judge
```

Every case in the dataset carries `expected_docs`, `expected_sections`,
`key_facts`, `forbidden_facts` and `expected_status`. Retrieval is scored against
the passages that came back; generation is scored separately by deterministic
fact matching, so a retrieval failure and a generation failure never average
together.

### Scenario 1 — Correct document, wrong chunk

**What fails.** *"What is the nightly hotel rate cap for London?"* The right
document is retrieved. The chunk holding `$350` is not.

**How it was diagnosed.** Not by tuning. The first step was to grep the chunk
text for the fact — and it was not there, which rules out chunk size and Top-K
(the number of passages retrieved) before either is touched. Inspecting the
parsers found three independent causes:

1. **PDF (Portable Document Format)** — `page.get_text()` emits a table column by
   column, so row association is destroyed *before* chunking is even reached.
2. **DOCX (Office Open XML document)** — `doc.paragraphs` skips tables entirely,
   appending them detached from the heading that gives them meaning.
3. **No morphological normalisation** in the lexical index, so *"approve"* scored
   zero against a table reading `Required Approver`, `approval`, `self-approve`.

**What was changed.** Table-aware parsing (`find_tables()` for PDF,
`body.iterchildren()` for DOCX, OOXML (Office Open XML) read directly for
spreadsheets so `15%` indexes as `15%` and not `0.15`); a `Title > Section`
breadcrumb prefixed to every chunk; and a suffix stemmer for the lexical index.

**How it was evaluated.** Category `table_lookup`, **n=6** — the largest sample
of any scenario here. The fixtures do the work: `expected_sections` names
`4. Hotel Accommodations`, which is what makes `section_hit` measurable at all,
and `forbidden_facts: ['250','180']` lists the *other two city caps*, so an
answer that quotes the wrong row of the right table is scored wrong rather than
partially right.

| Measure | Baseline | Improved |
|---|---:|---:|
| `table_lookup` correctness (n=6) | 83% | **100%** |
| `section_hit` | 0% | **85%** |
| `hit@1` | 78% | **100%** |
| MRR (Mean Reciprocal Rank) | 0.85 | **1.00** |
| Table chunks in the index | 0 | **22** |

The baseline's 0% on section hit is not a ranking failure — its chunks have no
sections at all, only character offsets like `chars 512-1024`.

### Scenario 3 — Similar documents, conflicting information

**What fails.** `Pricing2025.pdf` and `Pricing2026.pdf` are near-identical. Ask
what the Professional tier costs and similarity has no reason to prefer the
current one — if anything the 2025 card wins, being shorter and closer in wording
to the plain question. The user gets **$59** instead of **$65**, with a real
citation to a real document. This is the most dangerous failure in the system,
because the answer is indistinguishable from a correct one at the point of use.

**How it was diagnosed.** By elimination: recency is not a property of the text,
so no embedding or reranking change can recover it. It has to come from metadata
— and that metadata has to be resolved across the whole corpus, because whether
the 2026 card is current depends on whether a 2027 one exists.

**What was changed.** At ingest, supersession is resolved three ways in priority
order — an explicit forward link, an explicit backward link, and a filename
series (`Pricing2025` / `2026` / `2027`) — and re-run corpus-wide on every
ingest. At query time superseded chunks are **demoted, not deleted** (asking what
we charged in 2025 is legitimate), the demotion **inverts** when the question
names a past year, and the prompt labels every source `CURRENT` or `SUPERSEDED`
and forbids mixing their figures. Ordering matters: superseded duplicates are
filtered **before** reranking, so the reranker never spends a slot — or its
confidence — on a chunk about to be discarded.

**How it was evaluated.** Category `versioning`, **n=4**. The negative check is
the whole test: `key_facts: ['65']` with `forbidden_facts: ['59']`, so an answer
that hedges by quoting *both* prices scores wrong. Without that, hedging would
look like success.

| Measure | Baseline | Improved |
|---|---:|---:|
| `versioning` correctness (n=4) | 75% | **100%** |
| `citation_precision` | 87% | **100%** |

### Scenario 5 — Ambiguous query

**What fails.** *"What is the limit?"* matches expense category limits, hotel
rate caps, PTO (Paid Time Off) accrual caps, API call limits, an insurance
minimum and a discount cap. The baseline
picks one silently. The answer is not wrong — but the user asked about one thing
and was told about another, with no signal that a choice was made for them.

**How it was diagnosed.** By the shape of the retrieval, not the question: the
top hits scatter across two or more departments and three or more documents with
no clear winner, while the question itself carries a generic head (`limit`,
`cap`, `deadline`) and no qualifier to pin it down.

**What was changed.** Retrieve first, **then** ask — with the clarification
options drawn from what was actually retrieved, so they are real sections of real
documents rather than guesses. Condensation runs *before* the check, so a
follow-up that conversation context already resolves never reaches it.

**How it was evaluated.** Category `ambiguous`, **n=2** — the only cases in the
suite with **no** `expected_docs` at all. They are scored purely on
`expected_status: needs_clarification`, because the correct behaviour is to *not*
answer. Paired with `ambiguous_control`, **n=1** — `ambig-03`, *"What is the
expense limit for client gifts?"*, `expected_status: answered` — which exists
specifically **to measure the gate not firing**.

| Measure | Baseline | Improved |
|---|---:|---:|
| `ambiguous` (n=2) — gate should fire | 0% | **100%** |
| `ambiguous_control` (n=1) — gate should not fire | 100% | **100%** |

**Read this one with care:** 3 cases in total. `docs/evaluation.md` notes that a
single case is worth 2.9 points and differences under ~6 points are not signal.
The control is what makes it worth reporting at all — a clarification gate that
fires on answerable questions is worse than no gate.

### Scenario 6 — Conversational context

**What fails.** *"What about Starter?"*, asked after a question about Enterprise.
The baseline answered **"$49 per seat per month, with a minimum of 10 seats"** —
both figures fabricated. The real values are **$32** and **5**.

**How it was diagnosed.** By recognising two *opposite* failure modes.
Concatenating the conversation and embedding it degrades with length — by turn
three the retrieval query carries the text of two previous *answers*, so a
question about one tier retrieves chunks about another. Not concatenating leaves
the follow-up unretrievable, and the model fills the gap by inventing. The
baseline shows the second.

**What was changed.** Each follow-up is rewritten into a **standalone question**
and retrieval runs on that alone. Two rules stop the rewrite reintroducing the
pollution: only the last few turns are considered, and **only entities are
carried forward, never the content of prior answers**. Condensation is skipped
entirely when the question is already self-contained, which saves a model call
and — more importantly — stops the rewrite introducing errors into questions that
did not need it.

**How it was evaluated.** Category `followup`, **n=3** — the only cases carrying
a `history` array, so the harness replays the conversation turn by turn rather
than asking the question cold. `key_facts: ['32']` with `forbidden_facts: ['29']`
(the 2025 Starter price), so answering from the superseded card fails.

| Measure | Baseline | Improved |
|---|---:|---:|
| `followup` correctness (n=3) | 0% | **100%** |

---

Full write-ups for all six scenarios — including symptoms, the options
considered, and the cases that still fail — are in
[`docs/failure-scenarios.md`](docs/failure-scenarios.md).

---

## 6. Capabilities

| Capability | What it means | Where & how | When it kicks in | Status |
|---|---|---|---|---|
| **query rewriting** | Reshape the question before retrieving, so the search runs on something answerable | [`condense.py`](src/rag/retrieve/condense.py) rewrites a follow-up standalone, carrying entities only; [`decompose.py`](src/rag/retrieve/decompose.py) splits multi-hop into ≤3 sub-queries. Both self-skip; both fall back to heuristics | `condense` → `decompose` — the first two stages, before embedding | ✅ |
| **hybrid search** | Run lexical and vector retrieval together rather than choosing between them | [`local.py`](src/rag/store/local.py): BM25 (Best Matching 25) + cosine, fused by RRF on *ranks* not scores. [`azure_search.py`](src/rag/store/azure_search.py): one request, fused server-side | `search` | ✅ |
| **semantic ranking / reranking** | Re-order candidates with a model that reads question and passage together | [`rerank.py`](src/rag/retrieve/rerank.py) — three tiers, best available wins: Azure semantic ranker, else an LLM (Large Language Model) reranker over the top 20, else lexical | `rerank`, after `version_filter` | ✅ |
| **metadata filtering** | Narrow the searchable set by document attributes rather than by text | [`base.py`](src/rag/store/base.py) `SearchFilters` — five fields; an allow-list computed before scoring locally, OData evaluated server-side on Azure ¹ | inside `search` — the filter goes *in* the query, never after it | ⚠️ |
| **confidence scoring** | Attach a number to an answer, and act on it | [`guardrails.py`](src/rag/generate/guardrails.py) `compute_confidence()` — retrieval strength + top-two margin + citation validity → 0.0–1.0 | after `verify`; below `MIN_CONFIDENCE = 0.35` the answer is withdrawn | ✅ |
| **guardrails** | Refuse, clarify or withdraw rather than answer badly | [`guardrails.py`](src/rag/generate/guardrails.py) — sufficiency floor, ambiguity gate, refusal token, numeric grounding, citation validation, verification | gates between `version_rank` and `context`; verification at `verify`; then withdrawal | ✅ |
| **document-level access control** | Restrict what a caller can retrieve, per document | [`auth.py`](src/rag/api/auth.py) token → `Principal.departments`; [`pipeline.py`](src/rag/retrieve/pipeline.py) `_filters_for()` → `SearchFilters`. `/api/v1/documents` filters via `can_see()` ² | built before `search`, applied inside it — so unreadable chunks never take a top-k slot | ⚠️ |
| **caching** | Avoid paying twice for the same work | [`service.py`](src/rag/service.py) `AnswerCache` — 256 entries, keyed by profile + department scope + question, single-turn only. [`embeddings.py`](src/rag/providers/embeddings.py) `CachedEmbedder` — content-hash, persisted ³ | answer cache **before `condense`**; embedding cache inside `embed`, and inside ingest's `index` | ✅ |
| **automated RAG evaluation pipeline** | Measure quality repeatably, without hand-grading | [`run_eval.py`](eval/run_eval.py) — run, compare, or re-score saved runs with no new API calls; 35-case dataset; deterministic scoring plus an optional judge ⁴ | offline — never in the request path | ⚠️ |
| **Application Insights / production observability** | Ship traces and metrics somewhere queryable in production | [`tracing.py`](src/rag/observability/tracing.py) — structured JSON logs, one correlation id per request via `contextvars`, per-stage timings returned in the response ⁵ | correlation id at the middleware, before any stage; `stage()` wraps every stage above | ⚠️ |

¹ **metadata filtering** — the pipeline sets only `departments`; `doc_ids`,
`exclude_doc_ids`, `current_only` and `content_types` are implemented and never
set. Version filtering is separate, running *after* retrieval in
`prefilter_superseded`.

² **access control** — department-level, not per document. `doc_ids` /
`exclude_doc_ids` exist on `SearchFilters` but are never populated.

³ **caching** — the answer cache is per-replica and in-memory, so its hit rate
divides by replica count and resets on every deploy — gap 1 of the
[scale review](docs/scale-review.md#gap-register).

⁴ **evaluation** — no CI (continuous integration) workflow exists, so it runs on
demand rather than on every change.

⁵ **observability** — `APPLICATIONINSIGHTS_CONNECTION_STRING` is read into
`Settings` and consumed by nothing: there is no exporter and no OpenTelemetry
dependency.

**Six present, four partial, none absent.** The partials split into two kinds,
and the distinction matters for how much work each represents:

- **Mechanisms built but not wired** — metadata filtering and per-document access
  control. Both are small code changes: the filter fields already exist and both
  backends already translate them; the pipeline simply never populates them.
- **Things needing infrastructure, not code** — the evaluation pipeline needs a
  CI workflow; Application Insights needs an exporter, a dependency and a
  provisioned resource.

Item 10 is the one worth watching, because **its gap is invisible from
configuration alone**: the connection string is a documented setting that can be
set successfully, and nothing downstream reports that it goes nowhere.

---

## 7. Debugging Process

A runbook for one specific production report:

> *"The chatbot gives correct answers most of the time, but occasionally gives a
> completely wrong answer with a valid-looking citation."*

Written for the stack this repository actually runs on — **Azure OpenAI** for
generation and **Azure AI Search** for retrieval, both called directly over REST.

### What the symptom already tells you

**"Valid-looking citation" is itself the diagnostic.** It means the citation
markers resolved to real retrieved sources — `citations_valid: true` with an
empty `invalid_markers` — so this is *not* a fabricated-reference failure. And
because an answer was returned rather than withdrawn, it cleared every guardrail
including the confidence floor.

That narrows it to three causes. They look identical to the user and need
completely different fixes:

| Cause | What actually happened | How the trace tells them apart |
|---|---|---|
| **Wrong chunk of the right document** | The citation is genuine — the retrieved chunk was the wrong row or section of it | Cited hit's `section_path` / `content_type` is not where the answer lives; low `rerank_score`; small `sufficiency.margin` |
| **Superseded document** | The figure really *is* in that document. It is last year's. | `is_current: false` on a cited hit, an old `effective_date`, `recency_boost`, and the `versioning` block |
| **Figure absent from the cited source** | Genuine fabrication that slipped past the numeric check | `groundedness.unsupported_figures` non-empty, yet `confidence` still ≥ `MIN_CONFIDENCE` |

The middle one is the most likely and the most dangerous — see
[Scenario 3](#scenario-3--similar-documents-conflicting-information), where the
answer is *indistinguishable from a correct one at the point of use*.

### Step 1 — Recover the evidence

Every response carries an `X-Correlation-Id` header, and every log line emitted
while handling it carries the same `correlation_id`. The response body carries
the full per-stage trace (`include_trace`, default true) and per-hit scores
(`include_hits`).

**The constraint, stated plainly.** That trace lives in the response body and the
log stream and **nowhere else**. `APPLICATIONINSIGHTS_CONNECTION_STRING` is read
into `Settings` and consumed by nothing — there is no exporter (note ⁵ of
[Capabilities](#6-capabilities)). So for a query that has already run:

- if the caller kept the response body, you have everything;
- if not, you have the log stream, and only for as long as it is retained.

**The log-stream half of that was aspirational until recently.** The per-stage
trace and per-hit scores were assembled in `ask()` and returned in the response,
never written to stdout — which is why the table below lists only *incidental*
lines. `ask()` now also emits a single `answer trail` line carrying the whole
trace, the scored hits and their version currency, so a correlation id is
answerable from Log Analytics alone.
[§9](#9-tracing-a-live-query) has the queries.

Until an exporter exists, the cheap mitigation is still to have the chat client
retain `X-Correlation-Id` alongside any answer a user reports.

**Then grep the logs for that correlation id.** These lines each change the
diagnosis:

| Log line | What it means for this bug |
|---|---|
| `answer withdrawn after verification` | The guardrail *did* fire — whatever the user saw, it was not this request |
| `LLM rerank failed, used lexical fallback` | Ranking silently degraded; "wrong chunk" becomes far more likely |
| `semantic ranking unavailable` | The Azure AI Search semantic ranker is off — it needs Basic tier or above |
| `Azure OpenAI chat deployment did not respond` | Generation fell back to extractive; the answer was assembled, not written |
| `… throttled` | 429s from Azure OpenAI; retries changed timing and possibly which path ran |

### Step 2 — Read the trace

| Field | Reads as |
|---|---|
| `rerank_method` | `azure-semantic` · `llm` · `lexical` · `lexical-after-degenerate-llm`. Anything but the first two means the reranker never really ran |
| `sufficiency.margin` | Gap between the top two passages. **Near zero is the tell for this bug** — the ranker had no real winner and picked one anyway |
| `sufficiency.top_score` | Below `min_relevance` the system should have abstained; if it did not, check which backend produced the score |
| `groundedness.unsupported_figures` | Non-empty means a figure in the answer is not in any cited source — fabrication |
| `groundedness.citations_valid` / `invalid_markers` | Expected to be `true` / empty for this symptom; if not, it is a different bug |
| `versioning` | Whether a superseded twin was filtered before reranking, and whether the demotion inverted for a history question |
| `security_filter` | Which departments were in scope — a wrong answer can be a correct answer from the wrong scope |
| `condensed` | Whether the question was rewritten. A bad rewrite retrieves for a question the user never asked |
| `selected`, `context.dropped` | How many hits reached the prompt, and how many were dropped at the budget |

Per hit, `include_hits` gives `is_current`, `effective_date`, `rerank_score`,
`keyword_score`, `vector_score`, `rrf_score`, `recency_boost`, `matched_subquery`
and the chunk text — enough to see exactly which passage the model was reading.

### Step 3 — Reproduce it

Replay it from the command line, substituting the reported question:

```bash
python scripts/cli.py --show-hits --log-level INFO ask "What does the Professional tier cost per seat?"
```

- **Turn the answer cache off** — `RAG_ENABLE_ANSWER_CACHE=false`, or
  `use_cache: false` on the request — otherwise you may be re-reading the same
  cached answer rather than re-running the pipeline.
- **Expect near-determinism, not determinism.** `DETERMINISM_SEED` is sent on
  every temperature-0 call so runs are comparable, but it is a hint: Azure
  OpenAI's `system_fingerprint` changes when the backend does.
- **Isolate the layer.** Keyword-only and vector-only retrieval are first-class
  on the backend (`mode=keyword` / `mode=vector`), which answers "was the right
  chunk even retrievable?" — note these are **backend arguments, not command-line
  flags**; calling them takes a few lines against `backend.search(...)`, as
  [`scripts/verify_pipeline.py`](scripts/verify_pipeline.py) does.
- **If it does not reproduce**, compare `rerank_method` between runs. The
  reranker is the usual source of run-to-run variation — a model that scored a
  passage 10/10 on one call and 0/10 on the next is a failure already seen in
  this project.

**Then pin it down as a test.** Add the question to
[`eval/dataset.jsonl`](eval/dataset.jsonl) with `expected_docs`,
`expected_sections`, `key_facts`, and — the important one for this symptom —
**`forbidden_facts` naming the wrong value it produced**. Without that negative
assertion, an answer that hedges by quoting both the right and wrong figures
still scores as correct. Re-run that category alone to confirm it now fails, then
fix, then confirm it passes.

### Step 4 — Corrective action, by cause

| Cause | Fix | Then confirm with |
|---|---|---|
| **Wrong chunk** | If the fact never made it into a chunk, it is a parsing bug, not a retrieval one — check the chunk text first ([Scenario 1](#scenario-1--correct-document-wrong-chunk)). If it is in the index but ranked below distractors, the reranker is the lever, not a bigger Top-K | `section_hit` and `hit@1` on the affected category |
| **Superseded document** | Check supersession resolved at ingest, and that `is_current` was actually patched onto the old document's chunks — a demotion that never reached the index looks exactly like this | `versioning` category; the `patch` stage count in the ingest report |
| **Fabricated figure** | The numeric grounding check should have caught it. Establish whether it ran (`groundedness.method`) and whether confidence stayed above the floor anyway — if so, the floor is too low for this corpus | `hallucination_rate`, and the case you just added |

In every case, finish by re-running **both** profiles and confirming no
regression elsewhere. If you changed scoring rather than behaviour, use
`--rescore` so a metric change is never confounded with a generation change.

### Step 5 — What changes on the Azure backends

- **The reranker changes, and so does its scale.** Azure AI Search reports
  `rerank_method=azure-semantic` and scores 0–4, rescaled ×2.5. `min_relevance =
  4.0` and the ambiguity gate's `spread < 1.5` were calibrated against the 0–10
  output of the other reranker, so abstention behaviour must be re-measured —
  recorded in [`docs/evaluation.md`](docs/evaluation.md#what-these-numbers-do-not-show)
  and in [§8.6](#86--what-the-built-in-evaluators-cannot-see).
- **Check `is_current` in the live index.** A superseded document whose flag was
  never patched will look current to every query, producing precisely this
  symptom for every question that touches it.
- **Check for throttling before blaming the pipeline.** Azure OpenAI 429s trigger
  retries and fallbacks; a wrong answer during a throttling window may be a
  degraded path rather than a ranking fault.

---

## 8. Query Resolution & Evaluation Metrics

Two questions a reviewer asks in a demo, neither of which §5–§7 answers directly:
**what actually decides** that a question is vague, or that a document is out of
date, or that the corpus cannot answer at all — and once something goes wrong,
**which number do I read** to find out why.

### 8.1 — The model decides almost none of this

Four LLM (Large Language Model) calls exist in this pipeline — condense, rerank,
generate, verify — and **not one of them is ever asked *"is this question
ambiguous?"* or *"which of these two rate cards is current?"*** Those verdicts are
reached in Python, from the shape of the retrieved evidence and from metadata
resolved at ingest. The model's four jobs are narrower than they look:

| Call | What it is asked | What it is **not** asked |
|---|---|---|
| `condense` | Rewrite this follow-up as a standalone question | Whether the follow-up needed rewriting — a heuristic decides that, and skips the call |
| `rerank` | Score *"does this passage answer this question"*, 0–10 | Whether the best score is good enough |
| `generate` | Write the answer under nine rules | Which sources it may see, or whether to abstain |
| `verify` | Is each claim stated by a cited source | What to do about it |

The cheapest demonstration is to take the model away. With the Azure OpenAI
deployment unreachable, every LLM stage falls back to heuristics — and both gates
still fire correctly, at `llm_calls=0`:

```
$ python scripts/cli.py ask "What is the limit?"
[CLARIFY]  ... 3. Expense Categories & Limits — Business Expense Policy (Finance)
                2. What's Changed for 2026 — OrbitSuite Pricing 2026 (Sales)
                7. Additional Perks — Employee Benefits Guide (HR) ...
  llm_calls=0

$ python scripts/cli.py ask "What was the Professional tier price in 2025?"
[ANSWER]  confidence=0.79   cites Pricing2025.pdf — Professional $59
  llm_calls=0
```

The clarification options are still real sections of real documents, and the
2025 rate card is still the one promoted, because neither decision was the
model's to make.

That split is deliberate and it has an operational consequence: **a deterministic
decision is reproducible from the trace; a prompt rule is not.** The one place
this repo relies on a prompt rule instead of a gate — the invented "Standard
plan" — is also the one case that still fails intermittently, which is the point
[`docs/evaluation.md`](docs/evaluation.md#the-one-case-that-still-fails) makes as
*"a prompt rule is not a deterministic guardrail"*.

### 8.2 — The four hard question types

| Type | Signal actually used | Decided by | The model's role | Caller sees |
|---|---|---|---|---|
| **Ambiguous**<br>*"What is the limit?"* | Question shape **first** — ≤4 content terms, contains a generic head (`limit`, `cap`, `deadline`, `rate`, `policy`, `sla`…), ≤1 qualifier. Retrieval scatter **second** — ≥2 departments **or** ≥3 documents in the top 5 | [`detect_ambiguity`](src/rag/generate/guardrails.py) — a cascade of cheap exits; steps 1–3 never touch retrieval at all | **None in the decision.** It only *writes* the clarifying question, from options that are real `section — title (department)` labels, under `CLARIFY_SYSTEM`'s *"do not invent options"* | `needs_clarification` + up to 5 real sections |
| **Recency**<br>*"What does Professional cost?"* with two rate cards | `is_current` and `effective_date`, resolved corpus-wide at ingest by `reconcile_versions`; plus a year or change-word in the question, via [`detect_temporal_intent`](src/rag/retrieve/recency.py) | [`prefilter_superseded`](src/rag/retrieve/recency.py) **before** reranking, [`apply_version_ranking`](src/rag/retrieve/recency.py) after | Forbidden by answer-prompt **rule 7** from mixing `CURRENT` and `SUPERSEDED` figures. Never asked which is newer | Sources tagged `CURRENT` / `SUPERSEDED` with effective dates |
| **Not available**<br>*"What is the severance policy?"* | Four gates, cheapest first: rerank top score vs the `min_relevance` floor (**4.0**/10) → the `INSUFFICIENT_EVIDENCE` token → deterministic numeric grounding → claim-by-claim verification; blended by [`compute_confidence`](src/rag/generate/guardrails.py) and **withdrawn** below `MIN_CONFIDENCE` (**0.35**) | [`assess_sufficiency`](src/rag/generate/guardrails.py), [`check_numeric_grounding`](src/rag/generate/guardrails.py), [`verify_groundedness`](src/rag/generate/guardrails.py) | Emits `INSUFFICIENT_EVIDENCE` **verbatim** (rule 4) so abstention is machine-checkable, applies the entity-definition check (rule 6), and acts as the claim verifier | `insufficient_evidence` |
| **Conflicting** | Two distinct cases — see the table below | — | — | — |

Three things in that table are easy to read past, and each is the whole point:

- **Ambiguity is checked *after* retrieval and *after* condensation.** Retrieve,
  then ask — so every option offered is a section that exists, rather than a guess
  at what the user might have meant. And a follow-up that conversation history
  already resolves never reaches the check: *"what is the limit?"* after a
  question about expenses has already been rewritten into something specific.
- **Recency is the one type where similarity is structurally incapable of
  helping.** It is not a property of the text. No embedding, no reranker and no
  amount of `top_k` (the number of passages retrieved) recovers it — it has to
  arrive as metadata, reconciled corpus-wide, because whether the 2026 card is
  current depends on whether a 2027 one exists.
- **"Did retrieval return anything?" is not a test of whether an answer
  exists.** `top_k` is a fixed number, so a question about a policy that is not in
  the corpus still returns five confident-looking chunks about adjacent topics.
  The relevance floor tests the *scores*, not the row count.

**"Conflicting data" is two different problems, and only one is solved:**

| Case | Example | Status |
|---|---|---|
| **Version conflict** — one fact, two vintages | `Pricing2025.pdf` vs `Pricing2026.pdf` | ✅ Supersession metadata, superseded chunks filtered **before** reranking, prompt rule 7 forbidding mixed figures |
| **Genuine contradiction** — two **current** documents that disagree | Two live policies stating different notice periods | 🔲 **Not handled.** There is no conflict detector. Rule 5 (answer the part that is covered, say which part is not) is the nearest thing and it is not the same mechanism — the model would more likely pick one silently |

That second row is a real gap, stated here rather than omitted — the same
convention as the access-control omission reported in
[`docs/evaluation.md`](docs/evaluation.md#how-the-baseline-is-drawn).

### 8.3 — Where each decision lands in the trace

The stage sequence, with the three gates marked. This is what ties §8 back to the
§7 runbook — every verdict above is greppable in a response body:

```
condense → decompose → embed → search → version_filter → rerank → version_rank
                                             │                         │
                                             └── recency ──────────────┘
       → context → generate → verify
            │          │
            └──────────┴── ambiguity + sufficiency (both run at the start of
                           generation, because both decide from the retrieved
                           evidence rather than from the question text alone)
```

| Field | Where | Carries |
|---|---|---|
| `standalone_query` | response body | The question retrieval actually ran on, after condensation |
| `subqueries` | response body | How a multi-part question was split; each hit also carries `matched_subquery`, so you can see which half of a comparison it supports |
| `confidence` | response body | The blend of retrieval strength, margin, groundedness and citation validity |
| `condensed` | `trace` | Whether condensation rewrote the question at all, or self-skipped |
| `security_filter` | `trace` | The departments the caller was allowed to search |
| `rerank_method` | `trace` | `azure-semantic`, the LLM reranker, or the lexical fallback — i.e. **which scale the scores are on** |
| `versioning` | `trace` | `explicit_year`, `wants_history`, `dropped_superseded`, `boosted`, `demoted` |
| `selected` | `trace` | How many chunks actually reached the context window |

Per-stage timings and their own fields sit alongside — `decompose.subqueries` as
a count, `version_filter.*`, `rerank.method`, `version_rank.*`. The whole trace is
on by default (`include_trace`) and, as [§7](#7-debugging-process) records, it
lives in the response body and the log stream and **nowhere else** — Application
Insights receives nothing. Every field in this table is now also written to the
`answer trail` log line, which is what makes it recoverable from a correlation id
after the fact: [§9](#9-tracing-a-live-query).

### 8.4 — Azure RAG evaluation metrics, side by side

Azure AI Foundry's built-in RAG (Retrieval-Augmented Generation) evaluators, with
the plain-language reading of each next to it. The rightmost column is the one
that matters for adoption here: **the same questions are already being asked by
`eval/metrics.py` under different names**, so these are additive, not a rewrite.

| Azure evaluator | In plain words | Score | A low score means | First thing to change | Closest metric here |
|---|---|---|---|---|---|
| `builtin.retrieval` | *Did the search put the useful passages near the top?* | 1–5, LLM-judged on query + context | The right passages are in the candidate set but badly ordered — **or** the chunk is unreadable on its own | Chunking (heading breadcrumbs), hybrid + RRF (Reciprocal Rank Fusion), the rerank window | `hit@1`, `mrr` (Mean Reciprocal Rank), `section_hit` |
| `builtin.document_retrieval` | *A search-quality scorecard graded against labelled relevance judgments* | `ndcg@3` (Normalised Discounted Cumulative Gain), `xdcg@3`, `fidelity`, `top1_relevance`, `top3_max_relevance`, `holes`, `holes_ratio` | **`fidelity` low → a recall problem** — the good documents never entered the result set at all. **`ndcg@3` low while `fidelity` is fine → a ranking problem.** **`holes_ratio` high → your *labels* are incomplete, not your search** | Recall: parsing and ingestion. Ranking: rerank. Holes: label more documents before touching anything | `doc_recall`, `hit@5`, the `expected_docs` fixtures in [`eval/dataset.jsonl`](eval/dataset.jsonl) |
| `builtin.groundedness` | *Did it stick to the sources it was given?* | 1–5 | Hallucination — the answer went beyond the context | Answer-prompt rules 1–3, the `MIN_CONFIDENCE` withdrawal | `groundedness` — numeric check plus claim verifier |
| `builtin.groundedness_pro` | *A second opinion that tells you **which** claim was unsupported* | pass/fail + a `reason` string | Same as above — but the `reason` string is the actual triage tool. Read it before changing anything | — | `unsupported_figures`, `unsupported_claims` |
| `builtin.relevance` | *Did it answer the question that was actually asked?* | 1–5, query + response only — **no context** | It answered a **different** question. This is the classic ambiguity symptom | Condensation, decomposition, the ambiguity gate | `judge_score` |
| `builtin.response_completeness` | *Did it answer **all** of it?* | 1–5, needs ground truth | Half a multi-part question was silently dropped | Sub-query decomposition, round-robin context selection, `max_context_chars` | `key_facts_missing` |

The split worth internalising is Azure's own: `document_retrieval` is a
**component** evaluation — it grades the search engine against relevance labels
and never sees the answer — while `groundedness` / `relevance` /
`response_completeness` are **system** evaluations that grade the final response.
A retrieval failure and a generation failure need different fixes, so averaging
them hides which one you have. That is the same reason this repo scores retrieval
and generation separately.

### 8.5 — The triage grid

Read three numbers, get one diagnosis. This is the section to use in an incident;
the rest is background.

| `retrieval` | `groundedness` | `relevance` | Diagnosis | Where to look |
|:---:|:---:|:---:|---|---|
| high | **low** | – | The model is inventing **despite** good context | Answer prompt; the `MIN_CONFIDENCE` (0.35) withdrawal threshold |
| **low** | high | – | It answered faithfully — from the **wrong passage** | Parsing → chunking → hybrid → rerank, **in that order** |
| high | high | **low** | It answered a different question than the one asked | [`condense.py`](src/rag/retrieve/condense.py), [`decompose.py`](src/rag/retrieve/decompose.py), the ambiguity gate |
| **low** | **low** | **low** | The corpus probably does not contain it. **Check whether abstaining is the correct behaviour before "fixing" anything** | `min_relevance` (4.0), and the case's `expected_status` |
| high | high | high | …and the user still says it is wrong → **version conflict.** No built-in metric can see this | `is_current` and `effective_date` in the live index |

**Row 2 is the most useful line in this section**, and the order in it is not
cosmetic. It generalises Scenario 1: the London hotel rate cap was missing
because `page.get_text()` destroyed the table at parse time — not because
`top_k` was too small. Every hour spent tuning retrieval before checking that the
fact survived parsing is an hour wasted. **Grep the chunk text for the fact
first.**

### 8.6 — What the built-in evaluators cannot see

Five blind spots, each already evidenced somewhere in this repository. A metric
that scores a wrong answer 5/5 is worse than no metric, so these matter more than
the table above.

1. **Recency is invisible to every built-in evaluator.** An answer citing the
   superseded 2025 rate card scores `groundedness` 5/5, `relevance` 5/5 and
   `retrieval` 5/5 — it is faithful, relevant and well-retrieved, and it is
   wrong. Only ground truth catches it, which is exactly why every versioning case
   in [`eval/dataset.jsonl`](eval/dataset.jsonl) carries
   `forbidden_facts: ['59']` alongside `key_facts: ['65']`. **Without that
   negative check, an answer that hedges by quoting both prices scores as
   correct.**
2. **Correct abstention is *penalised*.** `INSUFFICIENT_EVIDENCE` scores about 1
   on relevance and completeness against any ground truth, so a system that
   abstains honestly looks worse than one that guesses fluently. Score abstention
   cases separately — as `abstention_accuracy` does here, measured **only** on the
   cases whose `expected_status` is not `answered`.
3. **Correct clarification is penalised the same way** — and optimising toward the
   metric produces a system that clarifies everything, which is worse than one
   that never clarifies. The fix is a control case that **must not** fire:
   `ambiguous_control`, *"What is the expense limit for client gifts?"*, exists
   purely to measure the gate **not** firing. See
   [§5 Scenario 5](#scenario-5--ambiguous-query).
4. **The evaluators are LLM-judged, and LLM judges drift.** `temperature=0` is not
   determinism; a `seed` is a hint, not a guarantee. During development this
   repo's own reranker scored every candidate 0/10 on one call and scored the
   right one 10/10 on the next, with identical input — which is why a degenerate
   all-zero rerank now falls back to the deterministic lexical scorer. **Treat a
   single evaluator run as a sample, not a measurement.**
5. **Thresholds do not transfer between rankers.** `min_relevance = 4.0` and the
   ambiguity gate's `spread < 1.5` were calibrated against a 0–10 LLM reranker.
   Azure AI Search's semantic ranker emits 0–4, rescaled ×2.5 —
   **rescaling a *range* is not the same as matching a *distribution*.** Both
   thresholds would then apply to a scorer they were never calibrated against, so
   abstention and clarification rates must be **re-measured** after any backend
   switch; which direction they move is not predictable from the current run.
   Also in [`docs/evaluation.md`](docs/evaluation.md#what-these-numbers-do-not-show)
   and [§7 Step 5](#step-5--what-changes-on-the-azure-backends).

### 8.7 — Status

🔲 **Not integrated.** There is no `azure-ai-evaluation` dependency and no
Foundry evaluation run — this section maps the Azure evaluator set onto the
metrics [`eval/metrics.py`](eval/metrics.py) already produces, so the two can be
reconciled if a Foundry project is provisioned. Closing it means adding the
package, exporting `eval/results/*.json` into the evaluator input schema, and
re-running; the existing `--rescore` flag exists precisely so a scoring change is
never confounded with a generation change.

What **is** available today without any Azure resource: the reranker's score
reaches the trace as `rerank_score` on every hit, `rerank_method` records which
of the three rerankers produced it, and `confidence` on every response is the
blend of retrieval strength, margin, groundedness and citation validity computed
by [`compute_confidence`](src/rag/generate/guardrails.py).

#### Which levers actually move confidence

That blend is a weighted sum, and the weights decide where effort is worth
spending. They are not documented anywhere else in this repository:

```
confidence = 0.45 · min(1, top_score / 10)      retrieval strength
           + 0.15 · clamp((top − second) / 4)   margin over the runner-up
           + 0.30 · groundedness                0.0 – 1.0
           + 0.10 · (citations_valid ? 1 : 0)
```

`top_score` and `second` are the **final** scores of hits 1 and 2 — where
`final = rerank_score + recency_boost`, which is **not** capped at 10.

**Worked example.** *"What is the nightly hotel cap in London?"* against
`Finance/TravelPolicy.docx` returns `confidence = 0.736`. Retrieval is correct —
the top hit is the 214-character tier table containing `London → $350` — yet the
number looks unremarkable. The diagram shows why, and what can be done about it:

```
                     confidence = 0.736          target: > 0.90
                                │
   ┌────────────┬───────────────┴───────────────┬────────────────┐
   │            │                               │                │
retrieval    margin                       groundedness       citations
 w = 0.45    w = 0.15                       w = 0.30          w = 0.10
min(1,top/10) clamp(Δ/4)                     0.0 – 1.0          0 or 1
   │            │                               │                │
 6.0/10       1.75/4                           1.0              true
 = 0.600      = 0.438                          = 1.0            = 1.0
   │            │                               │                │
 → 0.270      → 0.066                         → 0.300          → 0.100
   │            │                               │                │
▲ SCOPE      ▲ SCOPE                        ■ AT MAX         ■ AT MAX
  +0.180       +0.084                        no headroom      no headroom
   │            │
   └─────┬──────┘   both are functions of ONE number
         ▼
   rerank_score = 6.0        ← set by rerank_method
         │
   ┌─────┴──────────────────────────┬─────────────────────────────┐
   │                                │                             │
lexical (now)                  llm_rerank                  azure-semantic
0–10, term overlap             0–10, model-scored          0–4 ×2.5 → 0–10
capped at 6.00 here            BLOCKED: AOAI 403           needs Basic SKU +
                               (VNet rule)                 SEMANTIC=true, AND
                                                           can be disabled at
                                                           runtime by a failure
   │                                │                             │
   │                                └──────────────┬──────────────┘
   │                                               ▼
   │                                    rerank_score 8.5 – 10
   │                                    confidence 0.85 – 1.00  ✔
   ▼
 ✗ DEAD END — chunking the table
   bare row 2.50 (worse) · row+header 6.00 (no change)
   MAX_TABLE_CHARS = 3000 is already correct for a 214-char table
```

**Two of the four terms are already at maximum.** Groundedness is 1.0 and the
citations are valid — 0.40 of the 0.60 the grounding half can ever contribute.
No change to the corpus, the answer prompt or the verifier will move them,
because there is nothing left to move. **All remaining headroom (+0.264) sits in
`retrieval` and `margin`, and both are functions of `rerank_score`** — so there
is one lever here, not four.

The two move together, which is what makes the lever effective: a reranker that
raises the top score usually also opens the gap to the runner-up, so `retrieval`
and `margin` improve at once rather than independently.

| Scenario | top | #2 | retrieval | margin | Confidence |
|---|---|---|---|---|---|
| today (`lexical`) | 6.0 | 4.25 | 0.270 | 0.066 | **0.736** |
| better reranker, modest gap | 8.5 | 6.0 | 0.383 | 0.094 | 0.876 |
| better reranker, clear gap | 9.0 | 5.0 | 0.405 | 0.150 | **0.955** |
| decisive | 10.0 | 5.0 | 0.450 | 0.150 | **1.000** |

`margin` saturates once the gap reaches 4 points, so a reranker that is merely
*confident* about the right chunk already collects the full 0.15.

**Why 6.00 exactly, and why chunking cannot fix it.** `lexical_rerank` scores
`10 · (0.7 · body_coverage + 0.3 · heading_coverage)`. The query's content terms
are `night`, `hotel`, `cap`, `london` — and **`hotel` never appears in the table
body.** It is in the section heading *"4. Hotel Accommodations"*, which carries
0.3 weight over a denominator of all four terms:

| Chunking | body | heading | score |
|---|---|---|---|
| **Whole table** (current) | 3/4 | 1/4 | **6.00 / 10** |
| Bare row, no header | 1/4 | 1/4 | **2.50 / 10** |
| Row + repeated header, as [`_split_table`](src/rag/ingest/chunker.py) does | 3/4 | 1/4 | **6.00 / 10** |

Splitting the table either halves the score or changes nothing — `$350` without
its `Nightly Cap` column header means nothing, which is why the chunker keeps
tables whole below `MAX_TABLE_CHARS`.

**So the action is to stop falling back to `lexical`.** Ranked:

1. **Unblock Azure OpenAI.** `llm_rerank` scores 0–10 directly and would rate a
   table that literally answers the question far above the prose around it,
   lifting *both* retrieval and margin. It is unreachable while the resource's
   VNet rule returns 403 — the same fault that forces the local embedder, so one
   fix closes two problems.
2. **Enable Azure semantic ranking — and check it has not switched *itself*
   off.** Independent of Azure OpenAI: Basic SKU or above plus
   `AZURE_SEARCH_SEMANTIC=true`, and purpose-built for a short factual question
   against a small table. But configuration is not the only way to lose it:
   `AzureAISearchStore` degrades to plain hybrid when a semantic query fails, so
   a service with semantic enabled can still report `rerank_method: lexical`.
   That degrade used to be triggered by **any** search error and to last for the
   life of the process — so the 768/1536 vector mismatch above disabled semantic
   ranking as a side effect. It now fires only on Azure's own "semantic … not
   enabled" wording and expires after `SEMANTIC_RETRY_SECONDS`. `/health` →
   `index.semantic` reports the live state.
3. **Leave `lexical_rerank` alone.** Its `heading_coverage` denominator is every
   query term, so a three-word heading can never score well — that is what caps
   this case. But it is the *fallback*. Retuning it would raise the number
   without improving retrieval, and would hide that the intended reranker is not
   running.

---

## 9. Tracing a Live Query

§8 asks *"is the system right in general?"* and answers it offline, against
labels. This section asks the other question, the one that arrives as a support
ticket: **a user reports a bad answer and gives you a correlation id — what can
you actually recover?**

### 9.1 — What one correlation id yields

Every question writes a single `answer trail` line to stdout, which is the only
route into Log Analytics. It carries what the request did:

```
status, confidence, cache, total_ms, hit_count,
groundedness, groundedness_method, citations_valid,
rerank_method, versioning{explicit_year, wants_history, boosted, demoted},
grounded_in_superseded, cited_docs,
stages[] with per-stage timings, tokens, llm_calls, embedding_calls, providers,
hits[]: chunk_id, doc_id, department, section_path,
        is_current, version, effective_date,
        vector_score, keyword_score, rrf_score, rerank_score,
        recency_boost, score, matched_subquery
```

**The split is deliberate.** Scores, document ids and version currency are
operational data and are logged always. The question, the answer and chunk
snippets are *user content* and require `LOG_ANSWER_TRAIL=true` — turned on to
investigate, off again afterwards.

**Application Insights still receives none of this.**
`APPLICATIONINSIGHTS_CONNECTION_STRING` is read into `Settings` and consumed by
nothing; there is no `opentelemetry` or `azure-monitor` dependency (note ⁵ of
[Capabilities](#6-capabilities)). Log Analytics over container stdout is the
whole story, which is why the answer below is a query rather than a portal blade.

### 9.2 — The queries

**Request-level verdicts:**

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-rag-assistant"
| extend d = parse_json(Log_s)
| where tostring(d.correlation_id) == "<paste-the-id>"
| where tostring(d.message) == "answer trail"
| project TimeGenerated,
          question      = d.question,            // needs LOG_ANSWER_TRAIL
          standalone    = d.standalone_query,
          status        = d.status,
          confidence    = d.confidence,
          groundedness  = d.groundedness,
          stale_source  = d.grounded_in_superseded,
          rerank_method = d.rerank_method,       // azure-semantic | llm | lexical
          cited         = d.cited_docs,
          versioning    = d.versioning,
          total_ms      = d.total_ms,
          answer        = d.answer               // needs LOG_ANSWER_TRAIL
```

**Every retrieved chunk, every score, and its currency:**

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-rag-assistant"
| extend d = parse_json(Log_s)
| where tostring(d.correlation_id) == "<paste-the-id>"
| where tostring(d.message) == "answer trail"
| mv-expand hit = d.hits
| project doc_id     = tostring(hit.doc_id),
          section    = tostring(hit.section_path),
          department = tostring(hit.department),
          vector     = todouble(hit.vector_score),
          keyword    = todouble(hit.keyword_score),
          rrf        = todouble(hit.rrf_score),
          rerank     = todouble(hit.rerank_score),
          recency    = todouble(hit.recency_boost),
          final      = todouble(hit.score),
          current    = tobool(hit.is_current),
          version    = tostring(hit.version)
| order by final desc
```

**Why the scores stay separate rather than fused.** `rerank` null on every row
answers *"was the reranker applied at all?"*, and `rerank_method` names which of
the three produced them — i.e. **which scale the numbers are on**, the same point
[§8.3](#83--where-each-decision-lands-in-the-trace) makes about the trace. A
single fused score cannot answer either question.

### 9.3 — Which of §8.4's evaluators this answers

[§8.4](#84--azure-rag-evaluation-metrics-side-by-side) maps Azure AI Foundry's
RAG evaluators onto this repo's offline metrics. Here is the runtime counterpart
— what a live correlation id can and cannot tell you:

| §8.4 evaluator | From one correlation id? | Field, or why not |
|---|---|---|
| `builtin.groundedness` | ✅ | `groundedness`, `groundedness_method`, `citations_valid` |
| `builtin.groundedness_pro` | ✅ | `unsupported_figures`, `unsupported_claims` |
| `builtin.retrieval` (ordering) | ⚠️ partly | every per-chunk score is present, so *bad ordering* is visible; whether it was **the right passage** is not |
| `builtin.document_retrieval` | ❌ | needs relevance labels — `doc_recall`, `ndcg@3` come from [`eval/dataset.jsonl`](eval/dataset.jsonl) |
| `builtin.relevance` | ❌ | LLM-judged query-vs-response; nothing computes it in the request path |
| `builtin.response_completeness` | ❌ | needs ground truth; `key_facts_missing` is offline only |
| **source currency** | ✅ — **and Azure has no evaluator for it** | `grounded_in_superseded`, per-hit `is_current` / `version` |

**That last row is the point of this section.** Groundedness asks *"is the answer
supported by the context it was given?"* — it cannot ask whether that context was
still true. **A perfectly grounded answer can be false**, if it faithfully quotes
a rate card that has since been superseded. None of the built-in evaluators look
for this; this repo already detects it (`prefilter_superseded`,
`apply_version_ranking`, [§8.3](#83--where-each-decision-lands-in-the-trace)'s
`versioning` block), and the trail now surfaces it per request.

`grounded_in_superseded` is scoped to the documents actually **cited**, and the
distinction matters. Verified on both sides:

| Question | Retrieved | Cited | Flag |
|---|---|---|---|
| *"What is the 2026 pricing rate card?"* | `Pricing2025.pdf` at `is_current: false`, demoted `recency_boost: -1.0` | `Pricing2026.pdf` | **false** — version ranking working as designed |
| *"What were the 2025 list prices before the increase?"* | same document, boosted `+3.0` because `wants_history: true` | `Pricing2025.pdf` | **true** — and legitimately so |

Retrieval routinely surfaces an older version and demotes it; that is not a
fault. Only a *cited* stale document is a truthfulness signal.

### 9.4 — Alerting

Log-search alert rules over the same table — no exporter required:

```kusto
// answered from a superseded document, when the user did NOT ask for history
ContainerAppConsoleLogs_CL
| extend d = parse_json(Log_s)
| where tostring(d.message) == "answer trail"
| where tobool(d.grounded_in_superseded)
| where not(tobool(d.versioning.wants_history))
| summarize stale = count() by bin(TimeGenerated, 1h)
```

The `wants_history` guard is not optional — as the table above shows, a question
*about* 2025 **should** cite the 2025 document. Without it the rule fires on
every correct historical answer.

```kusto
// the reranker or the embedder silently changed underneath you
ContainerAppConsoleLogs_CL
| extend d = parse_json(Log_s)
| where tostring(d.message) == "answer trail"
| summarize count() by bin(TimeGenerated, 1h),
            rerank = tostring(d.rerank_method),
            embedder = tostring(d.providers.embeddings)
```

This one is worth wiring first. It reports `embedder=local-hashing` the moment a
fallback happens — the failure mode [§5](#5-rag-failure-scenarios) describes,
which otherwise surfaces as a vector-length error three layers away from its
cause.

### 9.5 — Status

⚠️ **Partial.** The `answer trail` line is implemented in
[`service.py`](src/rag/service.py) and verified locally: with the flag off and
on, on an abstention (no citations, so no false stale flag), and on both the
historical and current forms of a versioned question.

The **KQL is documented, not executed** — it targets a Log Analytics workspace
this work has not run against, the same caveat everything from §6.2 onward
carries in [`Deployment.md`](Deployment.md). And Application Insights remains
unwired, so the ⚠️ on the observability row in [§6](#6-capabilities) stands
unchanged: this improves what is *queryable*, not what is *exported*.

---

## Summary

| Deliverable | Status |
|---|---|
| 1. GitHub Repository | ✅ Done — 5 of 7 sub-items fully verified, 2 partial |
| 2. Architecture Diagram | ✅ Done |
| 3. Evaluation Results | ✅ Done |
| 4. Demo + Presentation Video | 🔲 TO DO |

Sections 5–9 are supporting analysis rather than items the brief asked for — the failure write-ups, the bonus-capability audit, the debugging runbook, the query-resolution/metrics map and the live-query trace. The brief lists four deliverables, which is why this table has four rows.

**The two partial items are both about live Azure resources, not about code.**
The Azure AI Search client and the Azure OpenAI embedding client are written and
exercised — against an offline REST stub and a local fallback embedder
respectively. Closing them needs a provisioned search service and an embedding
deployment, then a re-run of ingestion and evaluation; the live checklist is in
[`docs/pipeline.md`](docs/pipeline.md) and provisioning is scripted in
[`scripts/provision_azure_search.sh`](scripts/provision_azure_search.sh).

---
