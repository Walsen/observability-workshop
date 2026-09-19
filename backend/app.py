"""CDK app entry point for the X-Ray Sudoku Demo (task 10.1).

This is the ``--app`` target invoked by ``cdk synth`` / ``cdk deploy`` (see the
Justfile ``synth`` and ``deploy`` recipes). It constructs the CDK ``App``,
instantiates :class:`~backend.infra.sudoku_stack.SudokuStack`, and synthesizes.

The stack is pinned to **us-east-1** (Requirement 9.4) by passing an explicit
``env`` with only the region set. Account is left unset on purpose: an
environment that names an account or reads one from credentials would make CDK
perform context lookups against a live account, but the demo's synth must resolve
nothing from an account and run fully offline (no credentials, no network beyond
localhost). A region-only environment is "region-agnostic on account" — enough
to pin the region without triggering any lookup.
"""

from __future__ import annotations

import sys
from pathlib import Path

# cdk invokes this file as a script (`python backend/app.py`), so sys.path[0] is
# backend/, not the repo root, and the `backend.*` package would be unimportable.
# Put the repo root (this file's parent's parent) on sys.path first so the app
# resolves its own package the same way the test suite and Lambda runtime do.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import aws_cdk as cdk

from backend.infra.sudoku_stack import SudokuStack

_STACK_ID = "XraySudokuDemoStack"
_REGION = "us-east-1"


def main() -> None:
    """Build the CDK app, add the stack pinned to us-east-1, and synthesize."""
    app = cdk.App()
    # Region-only env pins us-east-1 (Requirement 9.4) without naming an account,
    # so synth performs no account context lookup and stays offline.
    SudokuStack(app, _STACK_ID, env=cdk.Environment(region=_REGION))
    app.synth()


if __name__ == "__main__":
    main()
