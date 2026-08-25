# Learning Objectives 

An enterprise knowledge assistant: a retrieval-augmented generation (RAG) system
over a mixed corpus of policy documents, spreadsheets and contracts, built on
Azure AI Search and Azure OpenAI. It answers questions from retrieved passages,
cites the source of every claim, trims results to the asker's department, and
abstains rather than guessing when the corpus cannot support an answer. This
file tracks the assignment's **Deliverables** section against what is actually
in the repository — each item linked to its evidence, each marked honestly.

## Contents

- [Status key](#status-key)
- [1. GitHub Repository](#1-github-repository)
- [2. Architecture Diagram](#2-architecture-diagram)
- [3. Evaluation Results](#3-evaluation-results)
- [4. Demo + Architecture Presentation Video](#4-demo--architecture-presentation-video)
- [Summary](#summary)
- [RAG Failure Scenarios](#rag-failure-scenarios)

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
| retrieval pipeline | ✅ | [`src/rag/retrieve/`](src/rag/retrieve/) — condense, decompose, hybrid retrieve + RRF (Reciprocal Rank Fusion), rerank, version-rank. All nine assignment stages verified 9/9 (52 checks) in [`docs/pipeline.md`](docs/pipeline.md) |
| Azure AI Search integration | ⚠️ | [`src/rag/store/azure_search.py`](src/rag/store/azure_search.py) — complete REST (Representational State Transfer) client: index definition with HNSW (Hierarchical Navigable Small World) vector profile and semantic configuration, native hybrid query, OData (Open Data Protocol) security filters, delete-by-document, metadata merge. Passes 20/20 contract checks — but **against the offline stub** [`scripts/_azure_search_stub.py`](scripts/_azure_search_stub.py), **never against a live service**. [`docs/pipeline.md`](docs/pipeline.md) carries the live-integration checklist |
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
| One or two RAG failure examples | 🔲 TO DO | **[§ RAG Failure Scenarios](#rag-failure-scenarios) below** — four written up for this purpose. Full set: [`docs/failure-scenarios.md`](docs/failure-scenarios.md), each with a symptom. [Scenario 1](docs/failure-scenarios.md#scenario-1--correct-document-wrong-chunk) is the strongest demo: a table destroyed at parse time |
| How you diagnosed them | 🔲 TO DO | The **How it was diagnosed** part of each [scenario below](#rag-failure-scenarios); also the *Root cause* and *Evidence* subsections in the full write-ups, plus the diagnostic ladder in [README § Step 5](README.md#step-5--architecture--problem-solving-answers) |
| Improvements implemented | 🔲 TO DO | The **What was changed** part of each [scenario below](#rag-failure-scenarios); the *The fix* subsection of each full write-up; the five profile branches in [`docs/evaluation.md`](docs/evaluation.md#what-is-being-compared) |
| Evaluation before vs after | 🔲 TO DO | [`docs/evaluation.md` § Headline](docs/evaluation.md#headline), [`eval/results/comparison.md`](eval/results/comparison.md) |
| What you would change before production deployment | 🔲 TO DO | [`docs/scale-review.md` § Gap register](docs/scale-review.md#gap-register) — ten ranked gaps; and [`docs/architecture.md` § Scale](docs/architecture.md#scale) |

The brief also notes: *"The candidate should personally explain the architecture
and technical decisions."*

---

## Summary

| Deliverable | Status |
|---|---|
| 1. GitHub Repository | ✅ Done — 5 of 7 sub-items fully verified, 2 partial |
| 2. Architecture Diagram | ✅ Done |
| 3. Evaluation Results | ✅ Done |
| 4. Demo + Presentation Video | 🔲 TO DO |

**The two partial items are both about live Azure resources, not about code.**
The Azure AI Search client and the Azure OpenAI embedding client are written and
exercised — against an offline REST stub and a local fallback embedder
respectively. Closing them needs a provisioned search service and an embedding
deployment, then a re-run of ingestion and evaluation; the live checklist is in
[`docs/pipeline.md`](docs/pipeline.md) and provisioning is scripted in
[`scripts/provision_azure_search.sh`](scripts/provision_azure_search.sh).

---

## RAG Failure Scenarios

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
rate caps, PTO (Paid Time Off) accrual caps, API (Application Programming
Interface) call limits, an insurance minimum and a discount cap. The baseline
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
