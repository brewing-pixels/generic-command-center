from __future__ import annotations

import secrets
from pathlib import Path


def ensure_token(path: Path) -> str:
    path = Path(path).expanduser()
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token + "\n", encoding="utf-8")
    path.chmod(0o600)
    return token
