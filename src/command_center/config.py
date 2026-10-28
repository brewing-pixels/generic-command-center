from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from ruamel.yaml import YAML

from .errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


class DaemonConfig(BaseModel):
    bind: str = "127.0.0.1"
    port: int = Field(default=4190, ge=1, le=65535)
    pin: str | None = Field(default=None, pattern=r"^\d{4,8}$")


class TaskStoreConfig(BaseModel):
    url: str = "http://127.0.0.1:4180"
    poll_seconds: int = Field(default=30, ge=1)


class OpenCodeConfig(BaseModel):
    binary: str = "opencode"
    agents_dir: str = "~/.config/opencode/agents"
    chat_server: str | None = None


class LocalModelConfig(BaseModel):
    start_script: str = "/path/to/local-model-start.sh"


class BudgetsConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1)
    gate_timeout_seconds: int = Field(default=900, ge=1)
    agent_timeout_seconds: int = Field(default=1800, ge=1)


class PolicyConfig(BaseModel):
    max_files: int = Field(default=200, ge=1)
    max_diff_bytes: int = Field(default=1_000_000, ge=1)


class ModelConfig(BaseModel):
    label: str
    model: str
    key_env: str | None = None
    health: str | None = None
    startable_when_free_mib: int = Field(default=15000, ge=0)
    context_window: int = Field(default=0, ge=0)


class Config(BaseModel):
    daemon: DaemonConfig = Field(default_factory=DaemonConfig)
    taskstore: TaskStoreConfig = Field(default_factory=TaskStoreConfig)
    state_dir: Path = Path("~/.local/state/command-center")
    worktree_root: Path = Path("~/.local/state/command-center/worktrees")
    token_path: Path = Path("~/.config/command-center/token")
    opencode: OpenCodeConfig = Field(default_factory=OpenCodeConfig)
    local: LocalModelConfig = Field(default_factory=LocalModelConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    models: dict[str, ModelConfig] = Field(default_factory=dict)
    roles: dict[str, str] = Field(default_factory=dict)
    config_path: Path | None = None

    @field_validator("state_dir", "worktree_root", "token_path", mode="after")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return Path(value).expanduser()

    def model_for_role(self, role: str) -> tuple[str, ModelConfig] | None:
        model_id = self.roles.get(role)
        if not model_id:
            return None
        model = self.models.get(model_id)
        if model is None:
            return None
        return model_id, model


def default_config_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    env = os.environ.get("CC_CONFIG")
    if env:
        return Path(env).expanduser()
    local = REPO_ROOT / "config.yaml"
    if local.is_file():
        return local
    user = Path("~/.config/command-center/config.yaml").expanduser()
    if user.is_file():
        return user
    raise ConfigError(
        "no config.yaml found; pass --config, set CC_CONFIG, or create "
        f"{user}"
    )


def load_config(path: str | Path | None = None) -> Config:
    config_path = default_config_path(path)
    if not config_path.is_file():
        raise ConfigError(f"config not found: {config_path}")
    yaml = YAML(typ="safe")
    try:
        data = yaml.load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface the file error
        raise ConfigError(f"{config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: expected a YAML mapping")
    try:
        config = Config.model_validate(data)
    except Exception as exc:  # noqa: BLE001 - surface the validation error
        raise ConfigError(f"{config_path}: {exc}") from exc
    config.config_path = config_path
    return config


def save_roles(config: Config, updates: dict[str, str]) -> None:
    """Write role defaults back to config.yaml, preserving comments."""
    path = config.config_path
    if path is None or not path.is_file():
        raise ConfigError("config path is unknown; edit config.yaml directly")
    unknown = {key: value for key, value in updates.items() if value not in config.models}
    if unknown:
        raise ConfigError(f"unknown model ids: {sorted(unknown.values())}")
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.representer.add_representer(
        type(None),
        lambda dumper, _data: dumper.represent_scalar(
            "tag:yaml.org,2002:null", "null"
        ),
    )
    data = yaml.load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a YAML mapping")
    roles = data.get("roles")
    if roles is None:
        from ruamel.yaml.comments import CommentedMap

        roles = CommentedMap()
        data["roles"] = roles
    for key, value in updates.items():
        roles[key] = value
    tmp = path.with_name(path.name + ".tmp")
    mode = path.stat().st_mode
    with tmp.open("w", encoding="utf-8") as handle:
        yaml.dump(data, handle)
    tmp.chmod(mode)
    tmp.replace(path)
    config.roles.update(updates)
