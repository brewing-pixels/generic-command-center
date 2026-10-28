from __future__ import annotations

from pathlib import Path

import pytest

from command_center.config import ConfigError, load_config


def test_load_config_expands_paths(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
state_dir: ~/.local/state/command-center
worktree_root: ~/.local/state/command-center/worktrees
token_path: ~/.config/command-center/token
taskstore:
  url: http://127.0.0.1:4180
models:
  hosted:
    label: Hosted model
    model: provider/model-name
    context_window: 131072
roles:
  coder: hosted
""",
        encoding="utf-8",
    )
    loaded = load_config(config)
    assert loaded.taskstore.url == "http://127.0.0.1:4180"
    assert loaded.state_dir.is_absolute()
    assert "~" not in str(loaded.state_dir)
    assert loaded.roles["coder"] == "hosted"
    assert loaded.models["hosted"].context_window == 131072
    assert loaded.model_for_role("coder") is not None


def test_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")
