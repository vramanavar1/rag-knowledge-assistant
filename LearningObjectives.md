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
| explain which changes improved which metrics | ✅ | [`docs/evaluation.md` § Where the improvement came from](docs/evaluation.md#where-the-improvement-came-from) — attributes each metric movement to a specific change. What the two profiles actually differ by is in [§ What is being compared](docs/evaluation.md#what-is-being-compared) |

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
| One or two RAG failure examples | 🔲 TO DO | [`docs/failure-scenarios.md`](docs/failure-scenarios.md) — six scenarios, each with a symptom. [Scenario 1](docs/failure-scenarios.md#scenario-1--correct-document-wrong-chunk) is the strongest demo: a table destroyed at parse time |
| How you diagnosed them | 🔲 TO DO | The *Root cause* and *Evidence* subsections of each scenario, plus the diagnostic ladder in [README § Step 5](README.md#step-5--architecture--problem-solving-answers) |
| Improvements implemented | 🔲 TO DO | The *The fix* subsection of each scenario; the five profile branches in [`docs/evaluation.md`](docs/evaluation.md#what-is-being-compared) |
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
