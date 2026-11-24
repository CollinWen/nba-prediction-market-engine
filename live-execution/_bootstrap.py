"""
Import bootstrap for the live-execution terminal.

The package directory is literally named ``live-execution`` (with a hyphen),
which is NOT a valid Python identifier, so ``import live-execution.core...``
can never work. Every module in this tree therefore calls::

    from _bootstrap import boot
    boot()

at the top, which:

  * puts ``live-execution/`` on ``sys.path`` so ``import config`` and
    ``import core.interfaces`` resolve, and
  * puts the repo root on ``sys.path`` so ``import kalshi_auth`` resolves.

This makes every file runnable directly from anywhere::

    .\\venv\\Scripts\\python.exe live-execution\\execution\\risk.py

No matter the current working directory.
"""
from __future__ import annotations
import sys
from pathlib import Path

_LIVE_EXEC = Path(__file__).resolve().parent          # .../live-execution
_REPO_ROOT = _LIVE_EXEC.parent                        # repo root (has kalshi_auth.py)


def boot() -> None:
    for p in (str(_LIVE_EXEC), str(_REPO_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
