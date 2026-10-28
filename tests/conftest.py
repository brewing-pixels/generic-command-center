from __future__ import annotations

from pathlib import Path

import pytest
from helpers import git

from command_center.config import Config


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
    (root / "command-center.yaml").write_text("version: 1\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")
    return root


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        state_dir=tmp_path / "state",
        worktree_root=tmp_path / "worktrees",
        token_path=tmp_path / "token",
        models={
            "hosted": {
                "label": "Hosted model",
                "model": "provider/model-name",
            },
            "local": {
                "label": "Local model",
                "model": "local/model-name",
            },
        },
        roles={"inquisitor": "hosted", "coder": "hosted", "reviewer": "hosted"},
    )
