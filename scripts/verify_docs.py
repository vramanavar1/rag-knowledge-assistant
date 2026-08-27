"""Run every command the documentation claims works, safely.

    python scripts/verify_docs.py
    python scripts/verify_docs.py --list      # show what would run, run nothing

Why this exists
---------------
`python -m rag.cli ...` was documented in four files and could never have worked
from a clean checkout -- the package lives under `src/`, which `-m` does not put
on `sys.path`. It shipped because nothing ever executed the commands the docs
promised. This does.

Why it is built this way
------------------------
The first version of this checker used a denylist of "commands that mutate the
environment". It listed `pip install` but not `python -m venv` -- so it happily
ran `python -m venv .venv` from the setup section, over an existing virtualenv,
with a different interpreter than the one that had built it. That rewrote the
venv's interpreter while leaving its compiled packages in place, and broke the
developer's environment with
`ModuleNotFoundError: No module named 'pydantic_core._pydantic_core'`.

A denylist fails the moment you forget an entry, so this version does not use
one. Two structural defences instead:

1. **It runs in a throwaway copy of the repository**, with ``RAG_DATA_DIR``
   pointed at a temp path. A command that mutates something cannot reach the
   real working tree, the real index, or ``.venv``.
2. **An allowlist decides what runs at all.** A command executes only if it
   matches a known read-only or idempotent shape. Everything else is reported
   as SKIP *with its reason* -- silent skipping is how the original broken
   command survived in the first place.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Only these shapes are ever executed. Anything else is skipped, by design:
# adding a command to the docs does not silently grant it the right to run.
ALLOW = [
    (r"^python --version$", "version check"),
    (r"^python -c \"import [\w, ]+; *print\(.*\)\"$", "import check"),
    (r"^python scripts/cli\.py .*\b(ask|compare)\b", "CLI query"),
    (r"^python scripts/ingest\.py(\s+--list|\s+--profile \w+|\s+--log-format \w+)*$",
     "ingest into the temp data dir"),
]

# Reasons for the common skips, so the report explains itself rather than
# leaving a reader to guess why a documented command was not exercised.
SKIP_REASONS = [
    (r"\bvenv\b|\buv\b|\bpip\b", "would mutate a Python environment"),
    (r"^docker\b|\bdocker \b", "builds or runs a container"),
    (r"^az\b|\baz \b|provision_azure", "needs an Azure subscription"),
    (r"Remove-Item|\brm\b|\bmv\b|Copy-Item|^cp\b", "deletes or moves files"),
    (r"--force", "rebuilds the working index"),
    (r"run_eval", "costs Azure OpenAI tokens"),
    (r"render_pdf", "needs Chrome and a Mermaid bundle"),
    (r"verify_pipeline|verify_lifecycle|verify_docs", "a suite of its own"),
    (r"^python -m uvicorn|^uvicorn\b", "long-running server"),
    (r"cli\.py chat", "interactive session"),
    (r"[<>]", "contains a placeholder"),
    (r"Set-ExecutionPolicy|Activate\.ps1", "changes shell state"),
    (r"^curl\b|^Get-ChildItem|^printf|^echo", "shell built-in or probe"),
]


# Lines that are shell *syntax* rather than a command you could run on its own:
# block delimiters, keywords, and variable assignments. They are not commands, so
# counting them would overstate how much of the documentation is executable --
# the opposite of what this script is for. A `$x = az ...` assignment is excluded
# too: the az call inside it is already covered by the SKIP rules, and the
# assignment as a whole is not a standalone command.
_NOT_A_COMMAND = re.compile(
    r"""^(
          [}{)\]]                              # a closing or opening block on its own
        | (if|else|elseif|foreach|for|while|function|param|return|try|catch|finally)\b
        | \$[\w:]+ \s* =                       # $Var = ... / $env:VAR = ...
        | \[void\]                             # [void]$errors etc.
        )""",
    re.VERBOSE,
)


def extract() -> list[tuple[str, int, str]]:
    """Every command line inside a shell fence, with its source location."""
    found: list[tuple[str, int, str]] = []
    files = sorted(REPO_ROOT.glob("*.md")) + sorted((REPO_ROOT / "docs").glob("*.md"))
    for md in files:
        lang, continued = None, False
        for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
            fence = re.match(r"^```(\w*)", line)
            if fence:
                lang = None if lang else (fence.group(1) or "text")
                continue
            if lang not in ("bash", "powershell", "sh", "shell"):
                continue
            cmd = line.strip()
            # A shell continuation is a fragment, not a command. Both characters
            # are needed: bash continues with a backslash, PowerShell with a
            # backtick. Miss the backtick and every continued line of an
            # `az containerapp create` block counts as a command of its own,
            # which inflates the total this script exists to report honestly.
            was_continued, continued = continued, cmd.endswith(("\\", "`"))
            if was_continued or not cmd or cmd.startswith(("#", ">")):
                continue
            if _NOT_A_COMMAND.match(cmd):
                continue
            found.append((md.relative_to(REPO_ROOT).as_posix(), lineno,
                          cmd.split("#")[0].strip()))
    return found


def classify(cmd: str) -> tuple[bool, str]:
    """(should_run, reason)."""
    for pattern, why in ALLOW:
        if re.match(pattern, cmd):
            return True, why
    for pattern, why in SKIP_REASONS:
        if re.search(pattern, cmd):
            return False, why
    return False, "not on the allowlist"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true",
                        help="show the classification without running anything")
    args = parser.parse_args()

    commands = extract()
    seen: set[str] = set()
    unique = [(p, n, c) for p, n, c in commands
              if not (c in seen or seen.add(c))]

    print(f"\n  {len(commands)} command(s) in the docs, {len(unique)} unique\n")

    if args.list:
        for path, lineno, cmd in unique:
            run, why = classify(cmd)
            print(f"  {'RUN ' if run else 'SKIP'}  {cmd[:64]:<64} {why}")
        return 0

    # A throwaway copy, so nothing a command does can reach the real tree.
    sandbox = Path(tempfile.mkdtemp(prefix="verify-docs-"))
    workdir = sandbox / "repo"
    shutil.copytree(
        REPO_ROOT, workdir,
        ignore=shutil.ignore_patterns(".git", ".venv", "data", "__pycache__",
                                      "*.pyc", ".playwright-mcp"),
    )
    env = {**os.environ,
           "RAG_DATA_DIR": str(sandbox / "data"),
           "PYTHONIOENCODING": "utf-8"}

    print(f"  sandbox: {workdir}\n")

    ran, skipped, failures = 0, 0, []
    for path, lineno, cmd in unique:
        should_run, why = classify(cmd)
        if not should_run:
            print(f"  SKIP  {cmd[:64]:<64} {why}")
            skipped += 1
            continue

        proc = subprocess.run(cmd, shell=True, cwd=workdir, env=env,
                              capture_output=True, text=True, timeout=300,
                              encoding="utf-8", errors="replace")
        ran += 1
        ok = proc.returncode == 0
        if not ok:
            failures.append((path, lineno, cmd, (proc.stderr or proc.stdout)[-400:]))
        print(f"  {'ok  ' if ok else 'FAIL'}  {cmd[:64]:<64} {path}:{lineno}")

    shutil.rmtree(sandbox, ignore_errors=True)

    print(f"\n  ran {ran}, skipped {skipped}, failed {len(failures)}")
    for path, lineno, cmd, err in failures:
        print(f"\n  FAILED {path}:{lineno}\n    $ {cmd}\n    {err.strip()[:400]}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
