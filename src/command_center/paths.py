from __future__ import annotations

from pathlib import Path


def project_root(path: str) -> Path | None:
    if not path:
        return None
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = root.resolve()
    if root.is_file():
        root = root.parent
    return root
