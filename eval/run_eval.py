"""Run the evaluation suite, and compare two runs.

    # measure the naive "before" system
    python eval/run_eval.py --profile baseline --out eval/results/baseline.json

    # measure the improved system
    python eval/run_eval.py --profile improved --out eval/results/improved.json

    # produce the before/after report
    python eval/run_eval.py --compare eval/results/baseline.json \\
                                     eval/results/improved.json \\
                                     --report eval/results/comparison.md

    # re-apply a scoring change to a saved run, without re-calling any model
    python eval/run_eval.py --rescore eval/results/baseline.json

Options
    --limit N        run only the first N cases (quick smoke)
    --category C     run only one category
    --no-judge       skip the LLM judge (deterministic scoring only)
    --repeat N       run each case N times and average latency
    --rescore FILE   recompute metrics from a saved run (no API calls), so a
                     scoring change is never confounded with a generation change

Both profiles must have been ingested first:
    python scripts/ingest.py --profile baseline
    python scripts/ingest.py --profile improved
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from metrics import (                                                 # noqa: E402
    NON_ANSWER_STATUSES,
    CaseResult,
    aggregate,
    expresses_decline,
    score_case,
)
from rag.config import get_settings                                   # noqa: E402
from rag.models import Principal, Turn                                # noqa: E402
from rag.observability.tracing import configure_logging               # noqa: E402
from rag.providers.llm import ChatProvider                            # noqa: E402
from rag.service import AssistantService                              # noqa: E402

DATASET = REPO_ROOT / "eval" / "dataset.jsonl"

JUDGE_SYSTEM = """You grade a knowledge assistant's answer against a reference answer.

Return JSON only: {"score": 0|1|2, "reason": "<12 words max>"}

2 = factually equivalent to the reference on every point asked
1 = partially correct, or correct but missing part of what was asked
0 = wrong, or invents information, or fails to answer when the reference does

Judge facts only. Ignore wording, length, formatting and citation markers.
If the reference says the correct behaviour is to decline or to ask for
clarification, then declining or asking scores 2 and answering scores 0."""


def load_cases(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def principal_for(department: str) -> Principal:
    if department in ("all", "", None):
        return Principal("eval", "Evaluator", ["*"], "admin")
    return Principal(
        f"eval-{department.lower()}", f"{department} evaluator", [department]
    )


async def judge(llm: ChatProvider, case: dict[str, Any], answer_text: str) -> tuple[int | None, str]:
    if not llm.available:
        return None, ""
    result = await llm.complete(
        [
            {"role": "system", "content": JUDGE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Question: {case['question']}\n\n"
                    f"Reference answer: {case['expected_answer']}\n\n"
                    f"Assistant answer: {answer_text}"
                ),
            },
        ],
        temperature=0.0,
        max_tokens=120,
        json_mode=True,
        utility=True,
    )
    parsed = result.json() if result else None
    if isinstance(parsed, dict) and "score" in parsed:
        try:
            return int(parsed["score"]), str(parsed.get("reason", ""))[:120]
        except (TypeError, ValueError):
            return None, ""
    return None, ""


async def run(args: argparse.Namespace) -> dict[str, Any]:
    os.environ["RAG_PROFILE"] = args.profile
    settings = get_settings(refresh=True)
    configure_logging(args.log_level, "text")

    service = await AssistantService.create(settings)
    stats = await service.backend.stats()
    if stats.chunks == 0:
        raise SystemExit(
            f"No index for profile '{args.profile}'. "
            f"Run: python scripts/ingest.py --profile {args.profile}"
        )

    cases = load_cases(DATASET)
    if args.category:
        cases = [c for c in cases if c.get("category") == args.category]
    if args.limit:
        cases = cases[: args.limit]

    print(f"\nprofile={args.profile}  backend={stats.backend}  "
          f"embeddings={stats.embedding_provider}  llm={service.llm.name}")
    print(f"index: {stats.documents} documents / {stats.chunks} chunks")
    print(f"cases: {len(cases)}\n")

    results: list[CaseResult] = []
    for index, case in enumerate(cases, start=1):
        history = [Turn(t["role"], t["content"]) for t in case.get("history", [])]
        principal = principal_for(case.get("department", "all"))

        latencies: list[float] = []
        answer = None
        error = ""
        for attempt in range(max(1, args.repeat)):
            started = time.perf_counter()
            try:
                answer = await service.ask(
                    case["question"],
                    history=history,
                    principal=principal,
                    use_cache=False,     # never measure the cache by accident
                )
            except Exception as exc:  # keep the suite running
                error = f"{type(exc).__name__}: {exc}"
                break
            latencies.append((time.perf_counter() - started) * 1000)

        if answer is None:
            result = CaseResult(
                id=case["id"], category=case.get("category", ""),
                difficulty=case.get("difficulty", ""), question=case["question"],
                department=case.get("department", "all"), error=error,
            )
            results.append(result)
            print(f"  {index:>2}/{len(cases)}  {case['id']:<12} ERROR {error[:70]}")
            continue

        latency = sum(latencies) / len(latencies)
        result = score_case(case, answer, latency, top_k=settings.context_top_k)

        if not args.no_judge:
            result.judge_score, result.judge_reason = await judge(
                service.llm, case, answer.text
            )

        results.append(result)

        mark = "ok  " if result.correct else "MISS"
        detail = []
        if result.key_facts_missing:
            detail.append(f"missing={result.key_facts_missing}")
        if result.forbidden_facts_present:
            detail.append(f"forbidden={result.forbidden_facts_present}")
        if result.forbidden_docs_cited:
            detail.append(f"bad-cite={result.forbidden_docs_cited}")
        if result.status != result.expected_status:
            detail.append(f"status={result.status}!={result.expected_status}")
        print(
            f"  {index:>2}/{len(cases)}  {result.id:<12} {mark} "
            f"{result.category:<22} {latency:6.0f}ms  {' '.join(detail)[:80]}"
        )

    summary = aggregate(results)
    payload = {
        "profile": args.profile,
        "backend": stats.backend,
        "embedding_provider": stats.embedding_provider,
        "llm": service.llm.name,
        "index": {"documents": stats.documents, "chunks": stats.chunks},
        "judge_enabled": not args.no_judge and service.llm.available,
        "summary": summary,
        "cases": [r.to_dict() for r in results],
    }

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {out}")

    print_summary(summary)
    return payload


def print_summary(summary: dict[str, Any]) -> None:
    def show(section: str, mapping: dict[str, Any]) -> None:
        print(f"\n  {section}")
        for key, value in mapping.items():
            if isinstance(value, dict):
                continue
            print(f"    {key:<32} {value}")

    print("\n" + "=" * 66)
    show("retrieval", summary["retrieval"])
    show("generation", summary["generation"])
    show("system", summary["system"])
    print("\n  by category")
    print(f"    {'category':<24} {'n':>3} {'correct':>8} {'hit@5':>7} {'halluc':>7}")
    for category, values in summary["by_category"].items():
        print(
            f"    {category:<24} {values['cases']:>3} "
            f"{_fmt(values['answer_correctness']):>8} "
            f"{_fmt(values['hit_at_5']):>7} "
            f"{_fmt(values['hallucination_rate']):>7}"
        )
    print("=" * 66)


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


# --------------------------------------------------------------------------
# Comparison report
# --------------------------------------------------------------------------

# Metrics where a lower number is the better result.
_LOWER_IS_BETTER = {"hallucination_rate", "latency_p50_ms", "latency_p95_ms",
                    "mean_prompt_tokens", "mean_completion_tokens",
                    "mean_llm_calls", "total_cost_usd", "cost_per_question_usd"}

# What changed in the improved profile that moves each metric. Stated up front
# so the report attributes a delta to a cause instead of just showing arrows.
ATTRIBUTION = {
    "hit_at_1": "structure-aware chunking + heading breadcrumbs + hybrid retrieval",
    "hit_at_5": "table-aware parsing (tables kept whole and attached to their section)",
    "section_hit": "section-aware chunking instead of fixed 512-character windows",
    "doc_recall": "sub-query decomposition for multi-hop questions",
    "mrr": "LLM reranking over the top-20 candidates",
    "answer_correctness": "all of the above, plus exact-figure prompting",
    "citation_correctness": "numbered per-chunk sources instead of free-text attribution",
    "citation_precision": "version-aware ranking drops superseded documents from context",
    "groundedness": "post-generation verification with a numeric grounding check",
    "hallucination_rate": "sufficiency gate + explicit refusal token + answer withdrawal",
    "status_accuracy": "abstention and clarification as first-class outcomes",
    "abstention_accuracy": "relevance floor on reranker scores",
    "latency_p50_ms": "cost of reranking, verification and query rewriting",
    "latency_p95_ms": "cost of reranking, verification and query rewriting",
    "mean_prompt_tokens": "larger, better-targeted context plus the verification pass",
    "total_cost_usd": "extra model calls for rerank, condense and verify",
}


def compare(baseline_path: Path, improved_path: Path, report_path: Path | None) -> None:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    improved = json.loads(improved_path.read_text(encoding="utf-8"))

    lines: list[str] = []
    add = lines.append

    add("# RAG Evaluation — Baseline vs Improved\n")
    add(f"- **Dataset**: {baseline['summary']['cases']} questions "
        f"across {len(baseline['summary']['by_category'])} categories")
    add(f"- **Baseline index**: {baseline['index']['chunks']} chunks "
        f"({baseline['index']['documents']} documents)")
    add(f"- **Improved index**: {improved['index']['chunks']} chunks "
        f"({improved['index']['documents']} documents)")
    add(f"- **Embeddings**: {improved['embedding_provider']} · "
        f"**LLM**: {improved['llm']}")
    add(f"- **LLM judge**: "
        f"{'enabled' if improved.get('judge_enabled') else 'disabled'}")

    for section, title in (
        ("retrieval", "Retrieval"),
        ("generation", "Generation"),
        ("system", "System"),
    ):
        add(f"\n## {title}\n")
        base_section = baseline["summary"][section]
        imp_section = improved["summary"][section]
        if section == "retrieval":
            add(f"_Measured on the {base_section['measured_on']} cases that name "
                f"an expected document; abstention, clarification and "
                f"access-control cases have none._\n")
        add("| Metric | Baseline | Improved | Change | What moved it |")
        add("|---|---:|---:|---:|---|")
        for key in base_section:
            if key == "measured_on":
                continue
            base_value, imp_value = base_section[key], imp_section.get(key)
            if base_value is None and imp_value is None:
                continue
            add(
                f"| {key} | {_cell(key, base_value)} | {_cell(key, imp_value)} | "
                f"{_delta(key, base_value, imp_value)} | "
                f"{ATTRIBUTION.get(key, '')} |"
            )

    add("\n## By category\n")
    add("| Category | n | Correct (baseline) | Correct (improved) | "
        "Hallucination (baseline) | Hallucination (improved) |")
    add("|---|---:|---:|---:|---:|---:|")
    for category, base_values in baseline["summary"]["by_category"].items():
        imp_values = improved["summary"]["by_category"].get(category, {})
        add(
            f"| {category} | {base_values['cases']} | "
            f"{_fmt(base_values['answer_correctness'])} | "
            f"{_fmt(imp_values.get('answer_correctness'))} | "
            f"{_fmt(base_values['hallucination_rate'])} | "
            f"{_fmt(imp_values.get('hallucination_rate'))} |"
        )

    # Per-case regressions and fixes are the most useful part of the report.
    base_cases = {c["id"]: c for c in baseline["cases"]}
    fixed, regressed = [], []
    for case in improved["cases"]:
        before = base_cases.get(case["id"])
        if not before:
            continue
        if case["correct"] and not before["correct"]:
            fixed.append(case)
        elif before["correct"] and not case["correct"]:
            regressed.append(case)

    add(f"\n## Cases fixed by the improvements ({len(fixed)})\n")
    if fixed:
        add("| Case | Category | Baseline answer | Improved answer |")
        add("|---|---|---|---|")
        for case in fixed:
            add(f"| `{case['id']}` | {case['category']} | "
                f"{_snippet(base_cases[case['id']]['answer'])} | "
                f"{_snippet(case['answer'])} |")
    else:
        add("_None._")

    add(f"\n## Regressions ({len(regressed)})\n")
    if regressed:
        add("| Case | Category | Why it now fails |")
        add("|---|---|---|")
        for case in regressed:
            reason = []
            if case["key_facts_missing"]:
                reason.append(f"missing {case['key_facts_missing']}")
            if case["status"] != case["expected_status"]:
                reason.append(f"status {case['status']}")
            add(f"| `{case['id']}` | {case['category']} | "
                f"{', '.join(reason) or 'see run output'} |")
    else:
        add("_None._")

    report = "\n".join(lines) + "\n"
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report, encoding="utf-8")
        print(f"wrote {report_path}")
    print("\n" + report)


def _cell(key: str, value: Any) -> str:
    if value is None:
        return "–"
    if isinstance(value, float) and 0.0 <= value <= 1.0 and "ms" not in key \
            and "token" not in key and "cost" not in key and "calls" not in key:
        return f"{value * 100:.0f}%"
    if isinstance(value, float):
        return f"{value:,.4g}"
    return str(value)


def _delta(key: str, base: Any, improved: Any) -> str:
    if base is None or improved is None:
        return "–"
    diff = improved - base
    if abs(diff) < 1e-9:
        return "="
    better = diff < 0 if key in _LOWER_IS_BETTER else diff > 0
    arrow = "▲" if diff > 0 else "▼"
    if isinstance(base, float) and 0.0 <= base <= 1.0 and 0.0 <= improved <= 1.0 \
            and "ms" not in key and "token" not in key and "cost" not in key \
            and "calls" not in key:
        text = f"{arrow} {abs(diff) * 100:.0f} pts"
    else:
        text = f"{arrow} {abs(diff):,.4g}"
    return f"**{text}**" if better else f"{text} ⚠"


def rescore(path: Path) -> None:
    """Recompute metrics from a saved run, without re-calling any model.

    Scoring rules get refined after a run -- the prose-decline detector was
    added once it was clear the baseline's status field understated it -- and
    re-running 35 questions through the API to apply a regex change is both
    slow and, because model output varies, not actually a controlled
    comparison. Rescoring keeps the model outputs fixed and changes only the
    judgement applied to them, so a metric change is never confounded with a
    generation change.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in load_cases(DATASET)}

    results: list[CaseResult] = []
    for stored in payload["cases"]:
        case = cases.get(stored["id"])
        if case is None:
            continue
        result = CaseResult(**{
            k: v for k, v in stored.items() if k in CaseResult.__dataclass_fields__
        })

        expected_status = case.get("expected_status", "answered")
        result.declined_in_prose = expresses_decline(result.answer)
        declined = result.status in NON_ANSWER_STATUSES or result.declined_in_prose

        if expected_status in NON_ANSWER_STATUSES:
            result.status_correct = declined
        elif expected_status == "needs_clarification":
            result.status_correct = result.status == "needs_clarification"
        else:
            result.status_correct = result.status == expected_status and not declined

        if expected_status == "answered":
            result.correct = (
                result.status == "answered"
                and not result.declined_in_prose
                and not result.key_facts_missing
                and not result.forbidden_facts_present
                and not result.forbidden_docs_cited
            )
        else:
            result.correct = (
                result.status_correct and not result.forbidden_facts_present
            )

        result.hallucinated = bool(
            (expected_status in NON_ANSWER_STATUSES and not declined)
            or result.forbidden_facts_present
        )
        results.append(result)

    payload["summary"] = aggregate(results)
    payload["cases"] = [r.to_dict() for r in results]
    payload["rescored"] = True
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"rescored {path} ({len(results)} cases)")
    print_summary(payload["summary"])


def _snippet(text: str, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    text = text.replace("|", "\\|")
    return (text[:limit] + "…") if len(text) > limit else text


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", choices=["baseline", "improved"], default="improved")
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--category", default=None)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--log-level", default="ERROR")
    parser.add_argument("--compare", nargs=2, metavar=("BASELINE", "IMPROVED"))
    parser.add_argument("--rescore", metavar="RESULTS_JSON",
                        help="recompute metrics from a saved run, no API calls")
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    if args.rescore:
        rescore(Path(args.rescore))
        return 0

    if args.compare:
        compare(
            Path(args.compare[0]),
            Path(args.compare[1]),
            Path(args.report) if args.report else None,
        )
        return 0

    await run(args)
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
