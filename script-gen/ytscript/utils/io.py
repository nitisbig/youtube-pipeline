"""Filesystem helpers. Unix friendly: atomic writes, explicit clobber policy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ..errors import OutputError


def write_text(path, content: str, force: bool = True) -> Path:
    """Atomically write ``content`` to ``path``.

    ``force=False`` refuses to overwrite an existing file (``--no-clobber``).
    """
    path = Path(path)
    if path.exists() and not force:
        raise OutputError(f"refusing to overwrite {path} (use --force)")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except OSError as exc:
        raise OutputError(f"cannot write {path}: {exc}") from exc
    return path


def read_text(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise OutputError(f"cannot read {path}: {exc}") from exc
