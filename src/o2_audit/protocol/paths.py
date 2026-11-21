"""Safe material path resolution.

Protocol §2: material paths must be safe relative paths under the directory
containing the task JSONL. Absolute paths, backslashes, parent jumps,
``:`` drive/URI syntax, empty parts and symlinks are rejected. Path checking
uses the vendored public ``safe_relative``; filesystem checks (symlink escape,
existence) happen here.

Note: ``page_index`` locates the page of the *source file*; it is never the
printed page number printed inside a document image. The printed page number,
when a rule needs it, is an ordinary observation predicate.
"""
from __future__ import annotations

import os
from pathlib import Path

from o2_public_contracts import contracts as public_contracts


class MaterialPathError(ValueError):
    pass


def resolve_material_path(task_dir: Path, relative_path: str) -> Path:
    """Resolve one material path under ``task_dir``; raise on any escape."""
    if not public_contracts.safe_relative(relative_path):
        raise MaterialPathError(f"unsafe material path: {relative_path!r}")
    candidate = (task_dir / relative_path).resolve()
    base = task_dir.resolve()
    if os.path.commonpath([str(candidate), str(base)]) != str(base):
        raise MaterialPathError(f"material path escapes task directory: {relative_path!r}")
    # Every path component must be a real entry, never a symlink (Windows and
    # POSIX): walk from base down to the candidate.
    probe = base
    for part in Path(relative_path).parts:
        probe = probe / part
        if probe.is_symlink():
            raise MaterialPathError(f"symlink in material path: {relative_path!r}")
    if not candidate.is_file():
        raise MaterialPathError(f"material file missing: {relative_path!r}")
    return candidate
