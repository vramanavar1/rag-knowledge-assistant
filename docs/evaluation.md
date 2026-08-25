# Evaluation — baseline vs improved
The deliverables table at the top of the [README](../README.md) indexes all five.
The brief itself is a private document and is not committed here.

The generated numbers live in
[`eval/results/comparison.md`](../eval/results/comparison.md); this document
explains how they were produced, what they mean, and what they do *not* mean.

```bash
python scripts/ingest.py --profile baseline
python scripts/ingest.py --profile improved
python eval/run_eval.py --profile baseline --out eval/results/baseline.json
python eval/run_eval.py --profile improved --out eval/results/improved.json
python eval/run_eval.py --compare eval/results/baseline.json \
                                  eval/results/improved.json \
                                  --report eval/results/comparison.md
```

---

## What is being compared

`RAG_PROFILE=baseline|improved` selects between two configurations of the **same
codebase**, run over the same corpus, the same 35-question dataset, the same
model and the same scoring. A before/after number is only as trustworthy as its
"before", so this is what the "before" is and how it was built.

### How the baseline is drawn

One rule at every decision point: **do the obvious thing a competent first pass
does, and omit what a first pass genuinely forgets.**

- Parse with the one-line call each library offers — `page.get_text()`,
  `doc.paragraphs`.
- Chunk at a fixed 512 characters. No overlap, no structure.
- Retrieve the top 5 by vector similarity, once, on the question as typed.
- Ask for an answer with citations, and stop there.

What it is *not* is handicapped in ways that would make the comparison
meaningless:

- **Same model, same context size.** `baseline_top_k = 5` and
  `context_top_k = 5` share one `max_context_chars = 12000` budget, so both
  profiles put **five chunks in front of the model**. The difference is *which*
  five, never how many.
- **It is still asked to cite its sources**, so citation accuracy stays
  comparable between the two — and the interesting result is that the baseline
  cites confidently while being wrong, which is the failure users actually
  report.
- **It is competent on easy questions**, scoring 80% on straightforward lookups.
  It falls over exactly where the assignment predicts, not everywhere.

One omission is deliberate rather than accidental: the baseline applies **no
access control**. Forgetting security trimming is part of what a first pass looks
like, so the evaluation reports it as a finding instead of quietly correcting it.

The matching argument about *scoring* fairness — the baseline has no abstention
mechanism, so it must be judged on what it actually said — is in
[One scoring correction worth stating](#one-scoring-correction-worth-stating).

#### Thumb-rule for drawing a baseline

Generalising from the above, for any before/after evaluation:

> **If you would not defend it in code review as a reasonable first attempt, it
> is a strawman, not a baseline.**

That is a judgement call, so here are five checks that make it testable — each
shown against how this baseline fares:

| # | Check | How this one fares |
|---|---|---|
| 1 | **Vary one layer; hold everything else constant.** Same model, corpus, dataset, scoring and context budget. If the baseline gets less context or a weaker model, the comparison measures *that*, not your improvements. | `baseline_top_k = 5`, `context_top_k = 5` and a shared `max_context_chars = 12000` — both profiles put five chunks in front of the same model. |
| 2 | **It must not lose everywhere.** A control beaten in every single category was rigged, not measured. | It **ties** the improved system in 4 of the 11 categories: `wrong_chunk` 100%/100%, `no_answer` 75%/75%, and both `_control` categories at 100%/100%. |
| 3 | **Omit only what a first pass genuinely forgets — then publish the omission as a finding**, rather than quietly correcting it. | No access control, so `access_control` scores 0% correct and 100% hallucination. Those numbers are reported, not hidden. |
| 4 | **Score both systems on what they actually produce**, never on a mechanism only one of them has. | Judging abstention by a `status` field the baseline does not implement overstated its hallucination rate by 9 points — see [One scoring correction worth stating](#one-scoring-correction-worth-stating). |
| 5 | **Freeze it once measured**, and re-apply any scoring change to *both* runs together. | `python eval/run_eval.py --rescore <results.json>` replays metric changes over saved runs without new API calls, so a metric change can never be confounded with a generation change. |

Check 2 is the one most often skipped, and the easiest to fail without noticing:
an improvement that wins everywhere usually means the control was crippled
somewhere it should not have been. Note the honest reading of this run — the
baseline **ties** in those four categories, it never beats the improved system
outright.

### What "improved" is, and how it is implemented

Not a second codebase. **One codebase with exactly five branches**, all keyed off
`Settings.is_baseline` in [`config.py`](../src/rag/config.py):

| # | Where | `baseline` | `improved` |
|---|---|---|---|
| 1 | [`ingest/sync.py`](../src/rag/ingest/sync.py) | `chunk_baseline()` over `naive_text`, fixed 512 chars | `chunk_document()` over the parsed block tree |
| 2 | [`retrieve/pipeline.py`](../src/rag/retrieve/pipeline.py) | `_retrieve_baseline()` — a single top-5 vector search | `_retrieve_improved()` — condense, decompose, hybrid + RRF, rerank, version-rank |
| 3 | [`generate/answer.py`](../src/rag/generate/answer.py) | ambiguity and sufficiency gates skipped | both gates run |
| 4 | [`generate/answer.py`](../src/rag/generate/answer.py) | the answer is returned whatever its confidence | withdrawn below `MIN_CONFIDENCE` (0.35) after verification |
| 5 | [`generate/answer.py`](../src/rag/generate/answer.py) | swaps in [`BASELINE_ANSWER_SYSTEM`](../src/rag/generate/prompts.py) — no refusal token, no rule on quoting figures exactly or on superseded documents | the full answer prompt |

**Parsing is deliberately not one of those branches**, and that is what makes the
chunking comparison valid. The parser always emits *both* representations — a
structured block tree and a `naive_text` dump, "what you get from the obvious
one-liner in each library" — and the profile chooses which to consume. One parse,
two views, so the measured difference is attributable to the chunking decision
rather than confounded with a second parse run.

Two more controls: the index and manifest are profile-scoped
(`index.baseline.json` / `index.improved.json`) so the two can never collide,
while the embedding cache is deliberately **shared** — the same chunk text under
the same model yields the same vector either way, which holds the embedding model
constant across the comparison.

### What that adds up to

| | `baseline` | `improved` |
|---|---|---|
| Parsing | `page.get_text()` / `doc.paragraphs` | table-aware, structure-preserving |
| Chunking | fixed 512 chars, no overlap | section-aware, breadcrumb-prefixed, tables kept whole |
| Chunks produced | 72 | 127 (22 of them tables) |
| Retrieval | top-5 pure vector | hybrid BM25 + vector, RRF fused |
| Ranking | none | LLM rerank over top-20, then version-aware |
| Query handling | verbatim | condensed, decomposed |
| Metadata | none used | department, version, effective date, currency |
| Guardrails | none | sufficiency, ambiguity, groundedness, withdrawal |
| Access control | none | department pre-filter inside the query |

---

## Headline

35 questions · 11 categories · Azure OpenAI `gpt-4o` · local hashed-feature
embedder (no embedding deployment was available on the test resource) ·
LLM judge enabled.

| | Baseline | Improved | |
|---|---:|---:|---|
| **Answer correctness** | 63% | **97%** | ▲ 34 pts |
| **Hallucination rate** | 11% | **3%** | ▼ 9 pts |
| **Abstention accuracy** | 38% | **88%** | ▲ 50 pts |
| **hit@1** | 78% | **100%** | ▲ 22 pts |
| **hit@5** | 93% | **100%** | ▲ 7 pts |
| **MRR** | 0.85 | **1.00** | ▲ 0.15 |
| **Section hit rate** | 0% | **85%** | ▲ 85 pts |
| **Citation correctness** | 89% | **100%** | ▲ 11 pts |
| **Groundedness** | 93% | **99%** | ▲ 6 pts |
| **LLM judge score** | 64% | **86%** | ▲ 21 pts |
| Latency p50 | 3.3 s | 5.9 s | ▲ 2.6 s |
| Cost per question | $0.0051 | $0.0098 | ▲ $0.0047 |

**15 cases fixed. 0 regressions.**

Correctness roughly halved the error rate three times over, and the two metrics
that matter most for trust — hallucination and abstention — moved furthest.
The cost is real and stated: answers take about 2.6 s longer and cost about
twice as much, because reranking, condensation and verification are three extra
model calls. Both of those are addressed below.

---

## Dataset

35 questions in [`eval/dataset.jsonl`](../eval/dataset.jsonl), written against
facts read out of the corpus:

| Category | n | What it probes |
|---|---:|---|
| `straightforward` | 5 | Plain prose lookups |
| `table_lookup` | 6 | Answers that live in tables (Scenario 1) |
| `wrong_chunk` | 3 | Right document, competing sections |
| `multi_hop` | 4 | Answers spanning sections, documents or spreadsheet tabs (Scenario 2) |
| `versioning` | 4 | 2025 vs 2026 rate cards (Scenario 3) |
| `no_answer` | 4 | Not in the corpus at all (Scenario 4) |
| `ambiguous` | 2 | A category named, not a value (Scenario 5) |
| `followup` | 3 | Multi-turn, elliptical (Scenario 6) |
| `access_control` | 2 | A caller asking outside their department |
| `ambiguous_control` | 1 | **Must not** trigger a clarification |
| `access_control_control` | 1 | **Must not** be refused |

The two control categories matter. A system that clarifies everything scores
100% on ambiguity and is useless; a system that refuses everything scores 100%
on abstention and is worse than useless. The controls make over-triggering
cost as much as under-triggering.

---

## How scoring works

Each case carries `expected_docs`, `expected_sections`, `key_facts`,
`forbidden_facts` and `expected_status`.

**Retrieval** is scored against the retrieved passages, independently of what
the model then wrote — a retrieval failure and a generation failure need
different fixes, and averaging them hides which one you have. Measured on the
27 cases that name an expected document; abstention, clarification and
access-control cases have none by design.

**Generation** is scored three ways, and the report shows all three rather than
picking a winner:

- *Deterministic key-fact matching*, with digit-boundary rules so "30" does not
  match "300" and "99" does not match "99.5". Cannot drift between runs.
- *`forbidden_facts`* — the negative check. A versioning question is correct
  only if the answer says `$65` **and does not say** `$59`. Without it, an
  answer that hedges by quoting both prices scores as correct.
- *An LLM judge* on a 0–2 scale, for phrasing the string match would miss.

**System** metrics are wall-clock latency, tokens taken from the Azure OpenAI
`usage` field (not estimated), and cost derived from those.

### One scoring correction worth stating

The first version of the metric counted "answered a question the corpus cannot
support" as a hallucination based purely on the response's `status` field.
That was unfair to the baseline: the baseline has no abstention *mechanism*, so
it always reports `answered` — but its prose sometimes says *"the provided
sources do not include information about the company's severance policy"*,
which is a correct refusal, and the judge scored it 2/2.

Both profiles are now scored on **what they actually said**
([`expresses_decline`](../eval/metrics.py)), not only on a status field the
baseline does not have. That moved the baseline's hallucination rate from 20%
down to 11% and its abstention accuracy from 0% up to 38% — a materially
smaller improvement to claim, and the honest one.

`python eval/run_eval.py --rescore <results.json>` re-applies scoring changes to
saved runs without re-calling the API, so a metric change is never confounded
with a generation change.

---

## Where the improvement came from

### Retrieval: parsing, not tuning

The single largest retrieval gain is `section_hit`, 0% → 85%. The baseline
scores zero not because it retrieves badly but because its chunks have no
sections at all — they are labelled `chars 512-1024`. That is the measurable
signature of the Scenario 1 root cause.

`hit@1` 78% → 100% and MRR 0.85 → 1.00 come from three changes that compound:
table-aware parsing puts the answer in a chunk at all; heading breadcrumbs make
that chunk interpretable alone; hybrid retrieval plus reranking puts it first.

The clearest single case:

> **"What is the nightly hotel rate cap for London?"**
> *baseline:* "The nightly hotel rate cap for London is **not explicitly
> mentioned in the provided sources.** However, bookings above the cap require
> pre-approval from a Director or above…"
> *improved:* "The nightly hotel rate cap for London is **$350**, excluding
> taxes and fees [1][2]."

The figure was in the document the whole time. `doc.paragraphs` had detached the
rate table from its heading and appended it at the end of the document.

### Generation: the categories that went from 0% to 100%

| Category | Baseline | Improved | What changed |
|---|---:|---:|---|
| `access_control` | 0% | 100% | Department pre-filter inside the query |
| `ambiguous` | 0% | 100% | Retrieve-then-ask, options drawn from real sections |
| `followup` | 0% | 100% | Standalone-question rewriting |
| `no_answer` | 0% | 75% | Relevance floor, refusal token, answer withdrawal |
| `multi_hop` | 50% | 100% | Sub-query decomposition + balanced context |
| `versioning` | 75% | 100% | Corpus-wide supersession, version-aware ranking |

The baseline failures in these categories are worth reading in full, because
they are the exact failures the assignment describes:

> **`acl-01`** — a **Sales** user asks about parental leave.
> *baseline:* "You are eligible for **12 weeks of paid parental leave**…"
> — HR content served to a caller with no HR access.
>
> **`follow-01`** — "What about Starter?" after a question about Enterprise.
> *baseline:* "The Starter tier is priced at **$49 per seat per month, with a
> minimum of 10 seats** [1]." — both figures fabricated; the real values are
> $32 and 5.
>
> **`noans-03`** — "What is the cancellation policy for the **Standard** plan?"
> *baseline:* "The cancellation policy for the Standard plan requires a
> 12-month contract term with auto-renewal…" — there is no Standard tier.

---

## The one case that still fails

`noans-03`, the invented "Standard plan", is the single remaining miss in the
improved profile (hence 75%, not 100%, on `no_answer`).

It is an adversarial lexical collision: the 2026 rate card contains the
sentence *"**Standard** contract term is 12 months with auto-renewal,
cancellable with 30 days' written notice"*. Every guardrail passes on it —
the reranker scores the chunk 10/10 because it genuinely does contain the words
"standard", "contract" and "cancellable"; the numeric check passes because 12
and 30 both appear in the cited source; and the claim-level verifier accepts it
because the sentence really is in the sources. Nothing is ungrounded. The
system has answered a question about a *different entity*.

An explicit prompt rule now handles it — *"if the question names a plan, tier
or product, first check that the sources define an entity by that name"* — and
the case passes when run in isolation. It fails intermittently in the full
suite, which is itself the honest finding: **a prompt rule is not a
deterministic guardrail.** The robust fix is entity-level verification —
extract the named entity from the question, require it to appear as a *defined*
term in the retrieved context, and abstain otherwise. That is on the list in
[the README](../README.md#what-i-would-change-before-production) and is not
implemented.

Also worth stating: `temperature=0` is not determinism. A `seed` is now sent
with every zero-temperature call, but Azure OpenAI treats it as a hint. During
development the reranker scored every candidate 0/10 on one call and scored the
right one 10/10 on the next with identical input — which is why a degenerate
all-zero rerank now falls back to the deterministic lexical scorer instead of
being trusted.

---

## The costs, stated plainly

Latency roughly doubled (3.3 s → 5.9 s p50) and cost roughly doubled
($0.0051 → $0.0098 per question). Both come from the same source: two extra
model calls per question, for reranking and verification.

On the test resource, **all four LLM stages run on `gpt-4o`**, because the
Azure OpenAI resource has no `gpt-4o-mini` deployment. Rerank, condense and
verify are ~75% of the model calls and, measured from the stage traces, ~78% of
the latency:

```
condense 0ms · decompose 0ms · embed 1ms · search 12ms · version_filter 0ms
· rerank 4267ms · version_rank 0ms · context 0ms · generate 1126ms · verify 1622ms
```

Pointing `AZURE_OPENAI_UTILITY_DEPLOYMENT` at a mini deployment is a one-line
change that recovers most of both regressions without touching answer quality —
the utility tasks are scoring and checking, not synthesis. That is the first
thing to do before any production deployment.

---

## What these numbers do not show

- **Retrieval ran on the local hashed-feature embedder**, not a real embedding
  model, because the test resource has no embedding deployment. It captures
  lexical and morphological overlap but has no notion of synonymy. The hybrid
  retriever and BM25 stemming are carrying more weight than they would with
  `text-embedding-3-small` behind them — and semantic paraphrase questions are
  under-represented in the dataset as a result.
- **35 questions is a small sample.** A single case is worth 2.9 points, so
  differences under ~6 points should not be read as signal.
- **The corpus is 11 documents.** Retrieval at 100% hit@5 says little about
  behaviour at 10,000 documents, where distractors are far denser.
- **Latency was measured against a corporate TLS-intercepting proxy** on a
  developer machine, so absolute numbers are pessimistic; the relative
  comparison is what holds.
