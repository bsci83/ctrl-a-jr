"""Guard against testing the WRONG checkout.

`ctrl_a_jr` is installed editable against ONE checkout. A pytest run started
inside a git worktree can import the MAIN checkout's source and report a green
suite over code this branch does not contain — an additive change would pass
over code that never executed. `pyproject.toml` sets `pythonpath = ["src"]` to
prevent that; this test proves it actually took effect rather than assuming it.
"""

from __future__ import annotations

from pathlib import Path

import ctrl_a_jr


def test_imported_package_is_this_checkout():
    pkg = Path(ctrl_a_jr.__file__).resolve()
    repo_root = Path(__file__).resolve().parent.parent
    assert repo_root in pkg.parents, (
        f"pytest imported {pkg}, which is OUTSIDE this checkout at {repo_root}; "
        "every other result in this run is about someone else's code"
    )
