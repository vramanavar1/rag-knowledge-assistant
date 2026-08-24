"""Command-line access to the assistant.

    python scripts/cli.py ask "What is the nightly hotel cap in London?"
    python scripts/cli.py --show-hits ask "What is the limit?"
    python scripts/cli.py chat --department Sales
    python scripts/cli.py compare "What does the Professional tier cost?"

After `pip install -e .` the same commands are available as `rag ask "..."`
and `python -m rag.cli ask "..."`. Without an install, use scripts/cli.py:
the package lives under src/, which `-m` does not put on sys.path.

``compare`` runs the same question through both profiles side by side, which is
the quickest way to see a failure scenario and its fix.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

if __package__ in (None, ""):  # allow `python src/rag/cli.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rag.config import get_settings
from rag.models import Answer, Principal, Turn
from rag.observability.tracing import configure_logging
from rag.service import AssistantService

DEPARTMENTS = ["Finance", "HR", "IT", "Legal", "Sales"]


def _principal(department: str | None) -> Principal:
    if not department or department == "all":
        return Principal(user_id="cli", display_name="CLI user",
                         departments=["*"], role="admin")
    return Principal(
        user_id="cli",
        display_name=f"CLI user ({department})",
        departments=[department],
        role="employee",
    )


def _render(answer: Answer, show_hits: bool = False) -> None:
    badge = {
        "answered": "ANSWER",
        "insufficient_evidence": "NO ANSWER",
        "needs_clarification": "CLARIFY",
        "denied": "DENIED",
    }.get(answer.status, answer.status.upper())

    print(f"\n[{badge}]  confidence={answer.confidence:.2f}")
    if answer.standalone_query and answer.standalone_query != "":
        print(f"  query -> {answer.standalone_query}")
    if len(answer.subqueries) > 1:
        for sub in answer.subqueries:
            print(f"    sub: {sub}")
    print()
    print(answer.text)

    if answer.citations:
        print("\nSources:")
        for citation in answer.citations:
            page = f" p{citation.page}" if citation.page else ""
            print(f"  {citation.marker} {citation.title} > "
                  f"{citation.section_path}{page}  ({citation.source_path})")

    if show_hits and answer.hits:
        print("\nRetrieved:")
        for index, hit in enumerate(answer.hits, start=1):
            chunk = hit.chunk
            print(
                f"  {index}. score={hit.score:>6.2f} "
                f"rerank={hit.rerank_score if hit.rerank_score is not None else '-':>5} "
                f"rrf={hit.rrf_score:.4f} " if hit.rrf_score else
                f"  {index}. score={hit.score:>6.2f} "
            )
            print(f"      {chunk.title} > {chunk.section_path} "
                  f"[{chunk.department}/{chunk.content_type}"
                  f"{'' if chunk.is_current else '/SUPERSEDED'}]")

    trace = answer.trace
    stages = trace.get("stages", [])
    if stages:
        timings = " ".join(f"{s['name']}={s['ms']}ms" for s in stages)
        tokens = trace.get("tokens", {})
        print(f"\n  {timings}")
        print(f"  total={trace.get('total_ms')}ms  "
              f"tokens={tokens.get('prompt', 0)}+{tokens.get('completion', 0)}  "
              f"llm_calls={trace.get('llm_calls', 0)}  "
              f"cache={trace.get('cache')}")


async def cmd_ask(args: argparse.Namespace) -> int:
    service = await AssistantService.create(get_settings(refresh=True))
    try:
        answer = await service.ask(args.question,
                                   principal=_principal(args.department))
    finally:
        await service.aclose()
    _render(answer, args.show_hits)
    return 0


async def cmd_chat(args: argparse.Namespace) -> int:
    service = await AssistantService.create(get_settings(refresh=True))
    principal = _principal(args.department)
    history: list[Turn] = []

    print(f"Chatting as {principal.display_name}. Ctrl-C or 'exit' to quit.\n")
    while True:
        try:
            question = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            await service.aclose()
            return 0
        if not question:
            continue
        if question.lower() in {"exit", "quit"}:
            await service.aclose()
            return 0

        answer = await service.ask(question, history=history,
                                   principal=principal)
        _render(answer, args.show_hits)
        print()
        history.append(Turn("user", question))
        history.append(Turn("assistant", answer.text))


async def cmd_compare(args: argparse.Namespace) -> int:
    for profile in ("baseline", "improved"):
        os.environ["RAG_PROFILE"] = profile
        settings = get_settings(refresh=True)
        print("=" * 72)
        print(f"PROFILE: {profile}")
        print("=" * 72)
        try:
            service = await AssistantService.create(settings)
            try:
                answer = await service.ask(
                    args.question, principal=_principal(args.department)
                )
            finally:
                await service.aclose()
            _render(answer, args.show_hits)
        except Exception as exc:
            print(f"  failed: {exc}")
        print()
    return 0


async def _main(argv: list[str] | None = None) -> int:
    # prog is derived from argv[0], so usage reflects however it was invoked
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--department", choices=[*DEPARTMENTS, "all"], default="all",
                        help="answer as a user of this department (security trimming)")
    parser.add_argument("--profile", choices=["improved", "baseline"], default=None)
    parser.add_argument("--show-hits", action="store_true")
    parser.add_argument("--log-level", default="WARNING")

    subparsers = parser.add_subparsers(dest="command", required=True)

    ask = subparsers.add_parser("ask", help="ask one question")
    ask.add_argument("question")
    ask.set_defaults(func=cmd_ask)

    chat = subparsers.add_parser("chat", help="interactive multi-turn chat")
    chat.set_defaults(func=cmd_chat)

    compare = subparsers.add_parser("compare", help="run baseline and improved")
    compare.add_argument("question")
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    if args.profile:
        os.environ["RAG_PROFILE"] = args.profile
    configure_logging(args.log_level, "text")
    return await args.func(args)


def main(argv: list[str] | None = None) -> int:
    """Sync entry point. The pipeline is async, so the loop starts here."""
    return asyncio.run(_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
