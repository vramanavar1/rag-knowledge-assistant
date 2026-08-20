# The six RAG failure scenarios

For each: what actually goes wrong on this corpus, the root cause found by
inspecting the pipeline rather than guessing, the fix, and the evidence.

Reproduce any of these side by side:

```bash
python scripts/cli.py compare "What was the Professional tier price in 2025?"
```

---

## Scenario 1 — Correct document, wrong chunk

### The symptom

Ask *"What is the nightly hotel cap in London?"*. The right document is
retrieved. The chunk containing `$350` is not.

### Root cause

Not chunk size. Not Top-K. **The table never survived parsing.**

Two independent bugs, one per file format, both confirmed by inspection:

**PDF.** `page.get_text()` emits a table column by column:

```
Category
Standard Limit
Approval Required Above Limit
Client meals
$100/person
Director
Team meals / offsites
$75/person
...
```

The row association is destroyed before chunking is even reached. No chunk size
recovers it, because the information is already gone. `page.find_tables()`
returns the grid intact:

```python
['Client meals', '$100/person', 'Director']
```

**DOCX.** `doc.paragraphs` skips tables entirely and `doc.tables` returns them
detached. Iterating the document body instead shows where they really sit:

```python
body order: ['p','p','p','p','p','p','p','p','tbl','p','p','p','tbl', ...]
tbl positions: [8, 12, 22]      # right after §3 Air Travel, §4 Hotel, §6 Meals
```

The naive read appends all three tables at the end of the document, so the hotel
rate caps (`$350 / $250 / $180`) end up orphaned from the *"4. Hotel
Accommodations"* heading that gives them meaning.

There is also a **third** cause, found while debugging retrieval rather than
parsing: the lexical index had no morphological normalisation, so *"who has to
approve this discount"* scored **zero** on BM25 against a table whose cells read
`Required Approver`, `self-approve` and `approval` — three surface forms of one
word, none equal to the query term.

### The fix

1. **Table-aware parsing.** PDF via `find_tables()`, with prose taken from the
   regions *outside* the detected table boxes; DOCX via `body.iterchildren()`
   interleaving paragraphs and tables in true reading order; XLSX read from the
   OOXML directly with number formats applied, so a 15% discount indexes as
   `15%` and not `0.15`.
   → [`src/rag/ingest/parsers.py`](../src/rag/ingest/parsers.py)
2. **Section-aware chunking that never splits a table**, with each chunk
   prefixed by a `Title > Section` breadcrumb plus department, effective date and
   version. This is the highest-leverage single change in the codebase. A chunk
   is retrieved *alone*; `Tier 1 | New York, San Francisco, Boston, London | $350`
   contains no word suggesting "hotel" or "nightly rate cap", and is unanswerable
   until the heading travels with it.
   → [`src/rag/ingest/chunker.py`](../src/rag/ingest/chunker.py)
3. **Hybrid retrieval + RRF**, so exact tokens (`$350`, `FIDO2`, `Net 30`) are
   reachable lexically even when the embedding blurs them.
4. **A stemming analyzer** for the lexical index, collapsing
   approve/approval/approver/self-approve to one term. Azure AI Search gets this
   from `en.microsoft`; the local backend implements it in
   [`src/rag/text.py`](../src/rag/text.py).
5. **Reranking** over the top 20, which is what actually decides *which* of the
   candidates answers the question.

### Evidence

22 table chunks are recovered across the 11 documents, versus 0 in the baseline
(whose chunker runs over the naive text dump).

```
Q: What is the nightly hotel rate cap for London?
   baseline: "The nightly hotel rate cap for London is not explicitly mentioned
              in the provided sources. However, bookings above the cap require
              pre-approval from a Director or above…"                    ✗
   improved: "The nightly hotel rate cap for London is $350, excluding taxes
              and fees [1][2]."                                          ✓
```

The figure was in the document the whole time — `doc.paragraphs` had detached
the rate table from its heading and appended it at the end.

Across the suite: `section_hit` 0% → 85%, `hit@1` 78% → 100%, MRR 0.85 → 1.00.
The baseline's 0% on section hit is not a ranking failure — its chunks have no
sections at all, only offsets like `chars 512-1024`.

After the stemming fix, the discount approval table entered the candidate set
for *"who has to approve it"* for the first time, and the answer changed from
*"no special approval is required"* to *"approval requires sign-off from the VP
of Sales"*.

---

## Scenario 2 — Information across multiple sections

### The symptom

*"If I travel to Chicago, what is my hotel cap and my dinner per diem?"* — the
answer needs §4 and §6 of the travel policy. *"For a 150 seat annual prepaid
contract, what discount applies and who approves it?"* needs three separate tabs
of the discount workbook.

### Root cause

One question produces one embedding, which lands *between* the two topics rather
than on either. Top-k then returns five half-relevant chunks. Worse, the terms
that pull one sub-topic up ("hotel", "Chicago") actively push the other down
("per diem", "dinner"), so a single query cannot rank both sources highly no
matter how large k is.

### The fix

**Sub-query decomposition** ([`decompose.py`](../src/rag/retrieve/decompose.py)):
detect comparative or multi-part questions, split into 1–3 independent lookups,
retrieve for each, union the results. An LLM does the split when available; a
regex-based splitter handles `compare X and Y`, `X vs Y`,
`difference between X and Y` and conjunctions otherwise.

**Balanced context selection** ([`pipeline.py`](../src/rag/retrieve/pipeline.py#L200)):
this is the part that is easy to miss. Taking a global top-5 after decomposition
defeats the purpose — for *"compare Enterprise and Starter"*, Enterprise chunks
routinely outscore Starter chunks across the board and would take all five
slots. Selection is round-robin across sub-queries, so each contributes before
any contributes twice.

Every hit records which sub-query found it (`matched_subquery`), so the UI and
the trace show which half of a comparison each piece of evidence supports.

### Evidence

```
Q: If I travel to Chicago, what is my nightly hotel cap and my dinner per diem?
   subqueries -> ['What is the hotel cap for Chicago?',
                  'What is the dinner per diem for Chicago?']
   improved: "The hotel cap for Chicago is $250 per night [1]. The dinner per
              diem for domestic travel, including Chicago, is $45 [2]."   ✓

Q: What is the receipt threshold for expenses, and what is the flight class for
   a 7 hour domestic flight for a manager?          (spans two documents)
   improved: "The receipt threshold for expenses is $25 … a manager on a 6+ hour
              domestic flight travels Economy Plus."                      ✓
   baseline: "For expenses, itemized receipts are required for reimbursement,
              particularly for meals…"       ✗ answered one half, dropped the other
```

Multi-hop goes 50% → 100%.

One detail that only showed up under review: the reranker scores its first 20
candidates, and with three sub-queries the merged candidate list can be 60 long.
A chunk that is rank 1 for the third sub-query but rank 25 by fused score would
never be scored at all — silently undoing the balanced selection downstream. The
round-robin interleave is therefore applied twice: once to build the rerank
window, and again to fill the context window.

---

## Scenario 3 — Similar documents, conflicting information

### The symptom

`Pricing2025.pdf` and `Pricing2026.pdf` are near-identical. Ask *"what does the
Professional tier cost"* and similarity has no reason to prefer one — if
anything the 2025 card wins, because it is shorter and its wording is closer to
the plain question (the 2026 card spends a section on what changed). The user
gets **$59** instead of **$65**, with a real citation to a real document.

This is the most dangerous failure in the system, because the answer is
indistinguishable from a correct one at the point of use.

### Root cause

Recency is not a property of the text, so no amount of embedding or reranking
can recover it. It has to come from metadata, and that metadata has to be
resolved across the whole corpus rather than per document.

### The fix

**At ingest** ([`metadata.py`](../src/rag/ingest/metadata.py)): resolve
supersession three ways in priority order — an explicit forward link
("…supersedes Pricing2025.pdf"), an explicit backward link ("See Pricing2026.pdf
for current rates"), and a filename-series convention (`Pricing2025` /
`Pricing2026` / `Pricing2027`, later year wins). The third is what keeps this
working when a new rate card arrives with no supersession sentence in it.

Critically, reconciliation is **corpus-wide and re-run on every ingest**, because
whether the 2026 card is current depends on whether a 2027 one exists. See
[ingestion-flow.md](ingestion-flow.md).

**At query time** ([`recency.py`](../src/rag/retrieve/recency.py)):

- Superseded chunks are **demoted, not deleted** — "what did we charge in 2025"
  and "what changed for 2026" are legitimate questions.
- If the question names a past year or uses change/history wording, the
  demotion **inverts**: chunks effective in that year are promoted instead.
- For an ordinary question, once a current document is covering the ground,
  superseded chunks are dropped from the context entirely, so the model cannot
  blend two rate cards into one answer.
- The prompt labels every source `CURRENT` or `SUPERSEDED` with its effective
  date, and forbids mixing their figures.

Note what this is *not*: filtering by date. An effective date in the past is
normal for a live policy. What matters is whether something has explicitly
replaced it.

### Evidence

```
Q: What was the Professional tier price in 2025?
   improved: "The Professional tier price in 2025 was $59 per seat per
              month [1]."                              cites Pricing2025.pdf  ✓
   baseline: "The Professional tier price in 2025 was not listed in the
              provided context. Only the Starter tier price of $29 per seat
              per month is mentioned for 2025 [2]."    cites Pricing2025.pdf  ✗

Q: Compare the Starter and Enterprise tiers on price per seat and uptime SLA.
   improved: "$32 … $109 …"                            cites Pricing2026 only ✓
   baseline: "In 2025, the Starter tier costs $29 … Enterprise $99 …"         ✗
             answers entirely from the superseded rate card
```

Versioning goes 75% → 100%, and citation precision 87% → 100% — the latter
because superseded documents stop reaching the context at all.

Two implementation details that were only obvious once measured:

**Superseded duplicates are filtered *before* reranking, not after.** When both
rate cards reach the reranker it has no basis to prefer one, and it was observed
scoring the **2025** tier table 10/10 and the 2026 one 0/10. Filtering
afterwards then discarded the only candidate the reranker was confident about,
leaving nothing above the relevance floor — so the system abstained on a
question it could answer. Filtering first means the duplicate never competes.

**"Compare" is not a history word.** An earlier version treated
compare/versus/difference as signals of a version comparison, which readmitted
the 2025 card for *"compare the Starter and Enterprise tiers"* — a question
about two tiers, not two years. Genuine version questions name a year, which is
detected separately.

---

## Scenario 4 — Hallucination / missing information

### The symptom

*"What is the cancellation policy for the Standard plan?"* There is no Standard
tier — the tiers are Starter, Professional, Enterprise and Enterprise Plus. The
baseline invents one:

> The Standard plan has a 12-month contract term with auto-renewal, and it can
> be canceled with 30 days' written notice prior to renewal. **[2]**

### Root cause

**Retrieval always returns rows.** Top-k is a fixed number, so a question about
a non-existent policy still yields five plausible chunks about adjacent topics.
"Did retrieval return anything?" is not a test of whether an answer exists.

### How sufficiency is determined

Four independent gates, cheapest first
([`guardrails.py`](../src/rag/generate/guardrails.py)):

1. **A relevance floor on reranker scores.** The reranker scores *"does this
   passage answer this question"* on 0–10, which is a different question from
   "is this passage similar". If the best passage scores below 4.0, the corpus
   probably does not contain the answer. This is the gate that fires most often.
2. **An explicit refusal token.** The model is instructed to emit
   `INSUFFICIENT_EVIDENCE` verbatim rather than to "say you don't know" — a
   sentinel is machine-checkable, a politely-worded non-answer is not, and the
   abstention rate is a measured metric.
3. **A deterministic numeric grounding check.** Every figure in the answer must
   appear in the cited source text. On this corpus nearly every dangerous
   hallucination is a wrong number, and a wrong number is cheap to catch without
   a model.
4. **A claim-by-claim verification pass** when a model is available, plus
   citation-marker validation.

These combine into a confidence score, and — importantly — **an answer that
fails verification is withdrawn, not caveated**. A wrong figure with a real
citation is worse than no answer, because at the point of use it is
indistinguishable from a right one.

### Evidence

```
Q: What is the company's severance policy?
   improved: insufficient_evidence — "I don't have enough in the knowledge base
             to answer that reliably, so I'd rather not guess."           ✓
   baseline: "The provided sources do not include information about the
             company's severance policy."                                 ✓ (in prose)

Q: How many vacation days do contractors based in India receive?
   improved: insufficient_evidence                                        ✓
   baseline: declines in prose                                            ✓

Q: What is the cancellation policy for the Standard plan?
   improved: insufficient_evidence                                        ✓
   baseline: "The cancellation policy for the Standard plan requires a
             12-month contract term with auto-renewal…" [2]               ✗ invented a tier
```

Abstention accuracy goes 38% → 88%, hallucination rate 11% → 3%.

Note the baseline is not uniformly bad here, and the evaluation says so: it has
no abstention *mechanism* — it always reports status `answered` — but its prose
declines correctly on three of the four cases. Scoring it purely on the status
field would have counted those as hallucinations and overstated the improvement
by nine points. Both profiles are scored on what they actually said; see
[evaluation.md](evaluation.md#one-scoring-correction-worth-stating).

The remaining failure is the Standard-plan case, which still slips through
intermittently. It is worth understanding why: the 2026 rate card contains the
sentence *"**Standard** contract term is 12 months with auto-renewal"*, so the
reranker legitimately scores that chunk 10/10, the numeric check passes (12 and
30 are both in the cited source), and the claim verifier accepts it — nothing is
ungrounded. The system has answered accurately about a *different entity*. A
prompt rule handles it most of the time; the robust fix is entity-level
verification, which is not implemented.

---

## Scenario 5 — Ambiguous query

### The symptom

*"What is the limit?"* matches expense category limits, hotel rate caps, PTO
accrual caps, API call limits, the $1,000,000 insurance minimum, and the 40%
discount cap. The baseline picks one silently:

> The limit depends on the category. For example, client meals have a standard
> limit of $100 per person… **[1]**

That answer is not wrong, but the user asked about something and got told about
something else, with no signal that a choice was made on their behalf.

### The decision: retrieve first, then ask

Three options were available — answer anyway, ask before retrieving, or infer
from conversation. The choice here is **retrieve, then ask, with the options
drawn from what was actually retrieved**, because:

- Asking *before* retrieving means guessing at what the user might have meant,
  and offering options the corpus may not even contain.
- Retrieval is cheap (~5 ms locally) and it makes the clarifying question
  concrete: the options are real sections of real documents.
- **Conversation context is used first, not last.** Condensation runs before
  ambiguity detection, so *"what is the limit?"* following a question about
  expenses is already rewritten into a specific question and never reaches the
  ambiguity check. Ambiguity is only reported when context genuinely fails to
  resolve it.

### Detection

A question is ambiguous when it names a *category* of value with no qualifier:

- Its content terms are dominated by generic heads — limit, cap, threshold,
  deadline, rate, policy, allowance…
- With **zero** qualifiers ("what is the limit?"), scatter across the retrieved
  hits settles it: two or more departments, or three or more documents. A
  decisive reranker score is deliberately *not* treated as evidence the question
  was understood — with no topic at all, a confident score just means the ranker
  picked one of several equally valid readings.
- With **one** qualifier ("what is the expense limit?"), the bar is higher: it
  must also be the case that the reranker failed to separate the candidates.

That asymmetry was added after the first implementation let *"what is the
limit?"* through: the reranker had confidently picked the expense table, so a
score-spread test alone said "not ambiguous".

### Evidence

```
Q: What is the limit?
   improved: needs_clarification
     - What's Changed for 2026 — OrbitSuite Pricing 2026 Rate Card (Sales)
     - Tuition Reimbursement — Employee Benefits Guide (HR)
     - Expense Categories & Limits — Business Expense Policy (Finance)
     - Volume Discounts — OrbitSuite Discount Schedule (Sales)
     - Limitation of Liability — Vendor Service Agreement (Legal)
   baseline: answered with expense limits only, silently                 ❌

Q: What is the expense limit for client gifts?   (control case)
   improved: answered — "$75 per recipient, Director approval above"     ✅
             correctly NOT treated as ambiguous
```

---

## Scenario 6 — Conversational context

### The symptom

```
User: What is the Enterprise plan cancellation policy?
User: What about Standard?
User: Is there any exception?
```

The naive approach — concatenate the conversation and embed it — degrades
monotonically with conversation length. By turn three the retrieval query is
carrying the text of two previous *answers*, so a question about one tier
retrieves chunks about another simply because those words are still in the
query.

The baseline shows the other failure mode, the one you get from *not*
concatenating: `"What about Starter?"` on its own retrieves badly, and the model
fills the gap by inventing **$39 per seat** — a price that appears nowhere in
the corpus.

### The fix

Rewrite each follow-up into a **standalone question** and retrieve with that
alone ([`condense.py`](../src/rag/retrieve/condense.py)). Two rules stop the
rewrite from reintroducing the pollution:

1. Only the last few turns are considered at all.
2. **Only entities are carried forward — never the content of prior answers.**
   The prompt is explicit: use the earlier *questions* for context, never copy
   facts, numbers or wording from earlier *answers*.

Condensation is **skipped entirely** when the question is already
self-contained, detected from elliptical openers ("what about…", "and…"),
pronouns, and length. That saves a model call on most turns and — more
importantly — stops the rewrite from introducing errors into questions that did
not need it.

Conversation history still reaches the *answering* prompt, for tone and pronoun
resolution. It just cannot reach *retrieval*.

### Evidence

```
history: "What is the Enterprise plan price per seat per month?"
         → "The Enterprise tier is $109 per seat per month…"
Q: What about Starter?
   improved: rewritten -> "What is the price per seat per month for the
                          Starter plan?"
             "The Starter tier is $32 per seat per month with a minimum
              of 5 seats [1]."                                            ✓
   baseline: "The Starter tier is priced at $49 per seat per month, with a
              minimum of 10 seats [1]."                    ✗ both figures fabricated

history: "How much PTO do I accrue after 4 years of service?" → "20 days"
Q: And what is the carryover?
   improved: "With 3-5 years of service, the maximum carryover for PTO
              is 5 days [1]."                                             ✓
   baseline: "The provided context does not include information about PTO
              accrual or carryover policies."                             ✗
```

The follow-up category goes 0% → 100%. The two baseline failures show both
halves of the problem: one fabricates when the bare follow-up retrieves badly,
the other simply gives up.

---

## Summary

| Scenario | Root cause | Primary fix | Measured |
|---|---|---|---|
| 1 — Wrong chunk | Tables destroyed at parse time; no stemming in the lexical index | Table-aware parsing, breadcrumb chunking, hybrid + rerank, stemming analyzer | table lookups 83% → **100%**; section hit 0% → **85%** |
| 2 — Multi-section | One embedding lands between two topics | Sub-query decomposition + round-robin interleave (twice) | multi-hop 50% → **100%** |
| 3 — Conflicting versions | Recency is not in the text | Corpus-wide supersession metadata; superseded filtered *before* reranking | versioning 75% → **100%**; citation precision 87% → **100%** |
| 4 — Hallucination | Retrieval always returns rows | Relevance floor, refusal token, numeric grounding, answer withdrawal | hallucination 11% → **3%**; abstention 38% → **88%** |
| 5 — Ambiguity | Category named, not a value | Retrieve-then-ask, options drawn from real sections | ambiguous 0% → **100%**, with the control case still answered |
| 6 — Follow-ups | History pollutes the retrieval query | Standalone-question rewriting, entities only | follow-ups 0% → **100%** |

Overall: answer correctness **63% → 97%**, 15 cases fixed, 0 regressions.
Full numbers and caveats in [evaluation.md](evaluation.md).
