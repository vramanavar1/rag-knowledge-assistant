# RAG Evaluation — Baseline vs Improved

- **Dataset**: 35 questions across 11 categories
- **Baseline index**: 72 chunks (11 documents)
- **Improved index**: 127 chunks (11 documents)
- **Embeddings**: local-hashing · **LLM**: azure-openai:vsquare-gpt-4o
- **LLM judge**: enabled

## Retrieval

_Measured on the 27 cases that name an expected document; abstention, clarification and access-control cases have none._

| Metric | Baseline | Improved | Change | What moved it |
|---|---:|---:|---:|---|
| hit_at_1 | 78% | 100% | **▲ 22 pts** | structure-aware chunking + heading breadcrumbs + hybrid retrieval |
| hit_at_5 | 93% | 100% | **▲ 7 pts** | table-aware parsing (tables kept whole and attached to their section) |
| section_hit | 0% | 85% | **▲ 85 pts** | section-aware chunking instead of fixed 512-character windows |
| doc_recall | 93% | 100% | **▲ 7 pts** | sub-query decomposition for multi-hop questions |
| mrr | 85% | 100% | **▲ 15 pts** | LLM reranking over the top-20 candidates |

## Generation

| Metric | Baseline | Improved | Change | What moved it |
|---|---:|---:|---:|---|
| answer_correctness | 63% | 97% | **▲ 34 pts** | all of the above, plus exact-figure prompting |
| answer_correctness_answerable | 70% | 100% | **▲ 30 pts** |  |
| citation_correctness | 89% | 100% | **▲ 11 pts** | numbered per-chunk sources instead of free-text attribution |
| citation_precision | 87% | 100% | **▲ 13 pts** | version-aware ranking drops superseded documents from context |
| groundedness | 93% | 99% | **▲ 6 pts** | post-generation verification with a numeric grounding check |
| hallucination_rate | 11% | 3% | **▼ 9 pts** | sufficiency gate + explicit refusal token + answer withdrawal |
| status_accuracy | 74% | 97% | **▲ 23 pts** | abstention and clarification as first-class outcomes |
| abstention_accuracy | 38% | 88% | **▲ 50 pts** | relevance floor on reranker scores |
| judge_score | 64% | 86% | **▲ 21 pts** |  |

## System

| Metric | Baseline | Improved | Change | What moved it |
|---|---:|---:|---:|---|
| latency_p50_ms | 3,291 | 5,939 | ▲ 2,648 ⚠ | cost of reranking, verification and query rewriting |
| latency_p95_ms | 5,217 | 8,916 | ▲ 3,698 ⚠ | cost of reranking, verification and query rewriting |
| mean_prompt_tokens | 1,657 | 2,708 | ▲ 1,051 ⚠ | larger, better-targeted context plus the verification pass |
| mean_completion_tokens | 92.86 | 301.4 | ▲ 208.5 ⚠ |  |
| mean_llm_calls | 2 | 3 | ▲ 1 ⚠ |  |
| total_cost_usd | 0.1775 | 0.3424 | ▲ 0.1649 ⚠ | extra model calls for rerank, condense and verify |
| cost_per_question_usd | 0.005072 | 0.009784 | ▲ 0.004712 ⚠ |  |

## By category

| Category | n | Correct (baseline) | Correct (improved) | Hallucination (baseline) | Hallucination (improved) |
|---|---:|---:|---:|---:|---:|
| access_control | 2 | 0% | 100% | 100% | 0% |
| access_control_control | 1 | 100% | 100% | 0% | 0% |
| ambiguous | 2 | 0% | 100% | 0% | 0% |
| ambiguous_control | 1 | 100% | 100% | 0% | 0% |
| followup | 3 | 0% | 100% | 0% | 0% |
| multi_hop | 4 | 50% | 100% | 25% | 0% |
| no_answer | 4 | 75% | 75% | 25% | 25% |
| straightforward | 5 | 80% | 100% | 0% | 0% |
| table_lookup | 6 | 83% | 100% | 0% | 0% |
| versioning | 4 | 75% | 100% | 0% | 0% |
| wrong_chunk | 3 | 100% | 100% | 0% | 0% |

## Cases fixed by the improvements (12)

| Case | Category | Baseline answer | Improved answer |
|---|---|---|---|
| `easy-05` | straightforward | The Vendor Service Agreement does not explicitly specify the duration for confidentiality obligations. However… | Confidentiality obligations under the Vendor Service Agreement survive for five (5) years after the terminatio… |
| `table-01` | table_lookup | The nightly hotel rate cap for London is not explicitly mentioned in the provided sources. However, bookings a… | The nightly hotel rate cap for London is $350, excluding taxes and fees [1][2]. |
| `multi-03` | multi_hop | In 2025, the Starter tier costs $29 per seat per month with an uptime SLA of 99.5%, while the Enterprise tier … | The Starter tier is priced at $32 per seat per month, while the Enterprise tier is priced at $109 per seat per… |
| `multi-04` | multi_hop | For expenses, itemized receipts are required for reimbursement, particularly for meals, which must include the… | The receipt threshold for expenses is $25; an itemized receipt is required for any single expense of $25 or mo… |
| `version-03` | versioning | The Professional tier price in 2025 was not listed in the provided context. Only the Starter tier price of $29… | The Professional tier price in 2025 was $59 per seat per month [1]. |
| `ambig-01` | ambiguous | The limit depends on the expense category. For example, client meals have a standard limit of $100 per person,… | Could you clarify which topic you are referring to? - What's Changed for 2026 — OrbitSuite Pricing — 2026 Rate… |
| `ambig-02` | ambiguous | The deadline depends on the context. For reporting employee benefits events, the deadline is within 30 days of… | Which deadline are you referring to? - Billing Terms — OrbitSuite Pricing — 2026 Rate Card (Sales) - Purpose —… |
| `follow-01` | followup | The Starter tier is priced at $49 per seat per month, with a minimum of 10 seats [1]. | The Starter tier is $32 per seat per month with a minimum of 5 seats [1]. |
| `follow-02` | followup | The provided context does not include information about PTO accrual or carryover policies. Could you provide t… | With 3-5 years of service, the maximum carryover for PTO is 5 days [1]. |
| `follow-03` | followup | The provided context does not contain information about monthly payment options for OrbitSuite or exceptions r… | Enterprise Plus tier requires annual prepaid billing and does not offer monthly billing as an option [1]. |
| `acl-01` | access_control | You are eligible for 12 weeks of paid parental leave following the birth, adoption, or foster placement of a c… | I don't have enough in the knowledge base to answer that reliably, so I'd rather not guess. The closest docume… |
| `acl-02` | access_control | The nightly hotel cap in San Francisco, which is classified as a Tier 1 city, is $350, excluding taxes and fee… | I don't have enough in the knowledge base to answer that reliably, so I'd rather not guess. The closest docume… |

## Regressions (0)

_None._
