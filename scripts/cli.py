"""Command-line entry point for the knowledge assistant.

    python scripts/cli.py ask "What is the nightly hotel cap in London?"
    python scripts/cli.py chat --department Sales
    python scripts/cli.py compare "What was the Professional tier price in 2025?"

Why this launcher exists
------------------------
The package lives under ``src/`` (a src layout), so ``python -m rag.cli`` fails
from a clean checkout with ``ModuleNotFoundError: No module named 'rag'`` --
``-m`` puts the *current directory* on ``sys.path``, never ``src/``. Rather than
make every reader remember to set ``PYTHONPATH`` (and remember that it is spelt
differently on PowerShell), this script does what ``scripts/ingest.py`` already
does: puts ``src`` on the path, then hands over to the real CLI.

Zero install, identical on every shell and platform.

If you would rather type less, ``pip install -e .`` installs the package and
gives you a ``rag`` command, after which ``rag ask "..."`` and
``python -m rag.cli ask "..."`` both work too.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rag.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
