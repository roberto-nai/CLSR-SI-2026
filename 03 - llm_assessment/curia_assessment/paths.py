"""Display filesystem paths relative to the project, independently of the caller's cwd."""

from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def display_path(path: str | Path) -> Path:
    """Keep absolute paths internal while presenting project-relative paths."""
    return Path(path).expanduser().resolve().relative_to(PROJECT_ROOT.resolve(), walk_up=True)


def display_error(error: Exception) -> str:
    """Render filesystem error filenames using the same project-relative convention."""
    if isinstance(error, OSError) and error.filename is not None:
        prefix = f"[Errno {error.errno}] " if error.errno is not None else ""
        message = f"{prefix}{error.strerror or type(error).__name__}: {str(display_path(error.filename))!r}"
        if error.filename2 is not None:
            message += f" -> {str(display_path(error.filename2))!r}"
        return message
    return str(error)
