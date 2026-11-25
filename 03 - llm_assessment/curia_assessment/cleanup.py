"""Clear generated outputs only when explicitly requested by the CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from .paths import PROJECT_ROOT


def clear_outputs() -> int:
    """Empty the project's fixed outputs directory and return removed child count.

    Existing experiment locks stop cleanup before any deletion. Directory links
    inside outputs are unlinked rather than followed, and outputs itself must
    be a real directory. Configuration, inputs and credentials are outside this
    fixed deletion scope.
    """
    outputs = PROJECT_ROOT.resolve() / "outputs"
    if outputs.is_symlink():
        raise ValueError("Cannot clear outputs: outputs is a symbolic link.")
    if not outputs.exists():
        outputs.mkdir()
        return 0
    if not outputs.is_dir():
        raise ValueError("Cannot clear outputs: outputs is not a directory.")

    contents = list(outputs.iterdir())
    pending = [outputs]
    while pending:
        directory = pending.pop()
        for child in directory.iterdir():
            if child.name == ".assessment.lock":
                relative = Path("outputs") / child.relative_to(outputs)
                raise ValueError(
                    f"Cannot clear outputs while an experiment is locked: {relative}. "
                    "Stop the running process before deleting its outputs.")
            if not child.is_symlink() and child.is_dir():
                pending.append(child)

    for child in contents:
        if child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child)
    return len(contents)
