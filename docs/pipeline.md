# Pipeline coverage — the nine canonical stages

The assignment specifies an exact pipeline shape. This document maps each stage
to the code that implements it, how it is verified, and its honest status.

```
Documents → Parsing → Chunking → Embeddings → Azure AI Search
          → Retrieval / Reranking → Context → LLM → Grounded Answer + Citations
```

Everything below is re-checkable, not a snapshot:

```bash
python scripts/verify_pipeline.py                  # local backend + Azure contract test
python scripts/verify_pipeline.py --backend azure  # whole pipeline on the Azure adapter
```

```
  stage                              checks  result  notes
  1 Documents                           4/4  PASS    11 documents · 5 departments
  2 Parsing                             4/4  PASS    294 blocks · 22 tables
  3 Chunking                            5/5  PASS    127 chunks · 22 tables
  4 Embeddings                          4/4  PASS    provider=local-hashing (local fallback) · 768 dims
  5 Azure AI Search                   20/20  PASS    verified against an offline REST stub (contract test)
  6 Retrieval / Reranking               4/4  PASS    5 chunks · azure-semantic
  7 Context                             3/3  PASS    5 sources · 1849 chars
  8 LLM                                 3/3  PASS    azure-openai:vsquare-gpt-4o · generator=llm
  9 Grounded Answer + Citations         6/6  PASS    status=answered · confidence=0.66 · 3519ms

  9/9 stages verified, 53 checks, 0 failure(s)
```

---

## Stage by stage

### 1 · Documents

| | |
|---|---|
| Code | [`ingest/manifest.py`](../src/rag/ingest/manifest.py) — `Manifest.scan()`, `file_hash()` |
| Does | Walks the source tree, hashes each file, classifies it new / modified / unchanged / deleted against the manifest |
| Verified | Every file classified · 5 departments discovered from the folder tree · all three formats present · SHA-256 recorded per document |
| Status | ✅ 11 documents, 5 departments, PDF + DOCX + XLSX |

Department comes from the folder, which is what a real deployment gets from the
Blob container or SharePoint library a file arrived in. It is the field the
security filter later keys on.

### 2 · Parsing

| | |
|---|---|
| Code | [`ingest/parsers.py`](../src/rag/ingest/parsers.py) — `parse_document()`, `parse_pdf/docx/xlsx` |
| Does | Structure-preserving extraction: PDF tables via `find_tables()`, DOCX via `body.iterchildren()` so tables stay inline, XLSX from the OOXML with number formats applied |
| Verified | Every document yields blocks · ≥ 20 table blocks recovered · **structured output differs from the naive dump for 11/11 documents** |
| Status | ✅ 294 blocks, 22 tables |

That third check is the load-bearing one. Each parser also produces a
`naive_text` rendering — what the obvious one-liner in each library returns —
and the verifier asserts the two differ. If someone regressed the parser back
to `page.get_text()`, this check fails rather than the pipeline quietly losing
every table. See [failure-scenarios.md](failure-scenarios.md#scenario-1).

### 3 · Chunking

| | |
|---|---|
| Code | [`ingest/chunker.py`](../src/rag/ingest/chunker.py) — `chunk_document()`, `build_embed_text()` |
| Does | Section-aware chunks that never split a table, each prefixed with a `Title > Section` breadcrumb plus department, effective date and version |
| Verified | 127/127 chunks prefixed with their document title · 117/117 sectioned chunks carry the full breadcrumb · 22 table chunks, none split · chunk ids identical on re-chunk |
| Status | ✅ 127 chunks |

The ten chunks without a `>` are front matter, which has no section by design
and carries the title alone. Chunk ids are `sha1(doc_id + section_path +
ordinal)`, so re-running ingestion converges rather than duplicating.

### 4 · Embeddings

| | |
|---|---|
| Code | [`providers/embeddings.py`](../src/rag/providers/embeddings.py) — `AzureOpenAIEmbedder`, `LocalEmbedder`, `CachedEmbedder` |
| Does | Embeds chunk text at ingest and query text at retrieval, through a content-hash cache |
| Verified | One vector per chunk · width matches the provider · vectors unit-normalised · a repeat call is served entirely from cache |
| Status | ⚠️ **Runs on the local fallback** |

`AzureOpenAIEmbedder` is implemented and probed at startup, but the test
resource has **no embedding deployment**, so the local hashed-feature embedder
is active. It captures lexical and morphological overlap but has **no notion of
synonymy** — "time off" will not match "PTO" the way a real embedding does.

The verifier prints which provider is live rather than passing silently, and
the startup log does the same. Deploying `text-embedding-3-small` is the
single largest outstanding quality lever in the system.

### 5 · Azure AI Search

| | |
|---|---|
| Code | [`store/azure_search.py`](../src/rag/store/azure_search.py) — `AzureAISearchStore`, behind the [`SearchBackend`](../src/rag/store/base.py) protocol |
| Does | Index definition (HNSW vector profile + semantic configuration + filterable metadata), document upsert, native hybrid + semantic query, delete-by-document, metadata merge |
| Verified | 20 checks against an offline stub of the REST API |
| Status | ✅ contract-verified · ⚠️ **not integration-verified** |

Exercised by [`scripts/_azure_search_stub.py`](../scripts/_azure_search_stub.py),
a stdlib HTTP server implementing the four endpoints the adapter calls, backed
by a real in-memory index so the assertions are about behaviour rather than
canned responses. What it proves:

| Check | Why it matters |
|---|---|
| `chunk_id` is the index key; vector field sized to the model; HNSW profile wired to its algorithm; semantic configuration declared; 11 filterable fields | The index definition is what makes hybrid + semantic + trimming possible at all |
| One request carries **both** `search` and `vectorQueries`, with `queryType: semantic` | Hybrid is native and server-fused, not two round trips |
| Department scope becomes `search.in(department, 'Sales', ',')` | Security trimming is an in-query pre-filter |
| An unscoped query reaches 3 departments; the scoped query returns 8 hits, **all Sales** | A broken filter would show as leaked results, not as zero results |
| `patch_document_fields` flips `is_current` and leaves `content_vector` intact | Demoting a superseded rate card must cost zero embedding calls |
| `delete_by_doc` removes exactly that document's chunks, others untouched | No orphans — the failure mode from [ingestion-flow.md](ingestion-flow.md) |
| A service that rejects `queryType: semantic` degrades once and keeps serving | Free-tier Azure AI Search has no semantic ranker |

**What it does not prove.** This is a contract test against the API as
documented. It cannot catch a divergence between my reading of the API and
Azure's actual behaviour. Before production, run this once against a real
service:

```bash
bash scripts/provision_azure_search.sh
export RETRIEVER_BACKEND=azure
export AZURE_SEARCH_ENDPOINT=https://<name>.search.windows.net
export AZURE_SEARCH_API_KEY=<admin-key>

python scripts/ingest.py --force        # creates the index, loads 127 chunks
python -m rag.cli ask "What is the nightly hotel cap in London?"
python -m rag.cli --department Sales ask "How many weeks of parental leave do I get?"
python eval/run_eval.py --profile improved --out eval/results/improved-azure.json
```

Expect: index created with 17 fields; `rerank_method=azure-semantic` in the
trace (Basic SKU or above); the Sales-scoped question declined; and evaluation
scores at or above the local backend's.

### 6 · Retrieval / Reranking

| | |
|---|---|
| Code | [`retrieve/pipeline.py`](../src/rag/retrieve/pipeline.py), [`rerank.py`](../src/rag/retrieve/rerank.py), [`recency.py`](../src/rag/retrieve/recency.py) |
| Does | Condense → decompose → security filter → hybrid retrieve + RRF → version prefilter → rerank → version rank → balanced selection |
| Verified | Expected document retrieved top-1 · every hit reranked with the method named · BM25 / vector / RRF signals recorded per hit · search and rerank timed separately |
| Status | ✅ `method=llm` on the local backend, `method=azure-semantic` on the Azure backend |

Note the backend difference the verifier surfaces: on Azure the service's L2
reranker supplies the scores, so the pipeline skips its own LLM rerank — 2
model calls instead of 3, and `rerank=0.0ms` instead of ~2s.

### 7 · Context

| | |
|---|---|
| Code | [`generate/prompts.py`](../src/rag/generate/prompts.py) — `build_context()`, `format_source()` |
| Does | Assembles the numbered source block, tagging each source CURRENT or SUPERSEDED with its effective date, within a character budget |
| Verified | Context is its own timed stage · sources numbered from 1 · stays within `max_context_chars` |
| Status | ✅ 5 sources, 1,849 / 12,000 chars |

`build_context` returns the hits that actually fit alongside the text, so a
citation marker can never point past the end of the window.

### 8 · LLM

| | |
|---|---|
| Code | [`providers/llm.py`](../src/rag/providers/llm.py), [`generate/answer.py`](../src/rag/generate/answer.py) — `_write()` |
| Does | Grounded synthesis against the numbered sources; also drives rerank, condensation, verification and the eval judge |
| Verified | Generation stage ran · completion returned with real `usage` tokens · auxiliary model calls accounted for |
| Status | ✅ live `gpt-4o` |

If no chat deployment is reachable the answer falls back to extractive
selection, and the verifier asserts that the fallback is *reported*
(`generator=extractive`) rather than passing as if a model had answered.

### 9 · Grounded Answer + Citations

| | |
|---|---|
| Code | [`generate/answer.py`](../src/rag/generate/answer.py), [`guardrails.py`](../src/rag/generate/guardrails.py) |
| Does | Emits one of four statuses; extracts citations; verifies groundedness; computes confidence; withdraws answers that fail verification |
| Verified | Status is one of the four · answer carries citations · **every citation resolves to a chunk that was in the context window** · every figure in the answer appears in the cited sources · groundedness scored · confidence computed |
| Status | ✅ 100% citation correctness and 99% groundedness across the 35-case suite |

The citation check is the one that matters for
[Step 5 Q6](../README.md#6-production-failure): citations point at numbered
chunks, not document names, so a claim can be checked against the exact text
that was in the window.

---

## Reading the trace against this list

Every answer carries a per-stage trace, and the stage names map directly onto
the assignment's diagram:

```
condense · decompose · embed · search · version_filter · rerank
         · version_rank · context · generate · verify
```

`embed` and `context` are timed separately from `search` and `generate`
precisely so this mapping is legible — and so embedding latency is visible,
which it was not when it sat inside the search stage.

Ingestion emits its own stage trace: `scan · parse · reconcile · purge ·
index · patch · persist`.

---

## Open items

1. **Deploy an embedding model** (stage 4). The largest quality lever available.
2. **Run the live Azure AI Search checklist** (stage 5) once a service exists,
   to convert a contract test into an integration test.
3. **Deploy a mini model** for the rerank / condense / verify calls — ~75% of
   the model calls and ~78% of the latency, at roughly a tenth of the cost.
