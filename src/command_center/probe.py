from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

import httpx

from .config import Config, ModelConfig


@dataclass(frozen=True)
class ModelStatus:
    id: str
    label: str
    model: str
    up: bool
    startable: bool
    reason: str


def _free_vram_mib() -> int | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return max(
            int(line.strip()) for line in result.stdout.splitlines() if line.strip()
        )
    except ValueError:
        return None


class ModelProbe:
    def __init__(self, config: Config, timeout: float = 2.0):
        self.config = config
        self.timeout = timeout

    def check_all(self) -> list[ModelStatus]:
        return [
            self._check(model_id, model)
            for model_id, model in self.config.models.items()
        ]

    def check(self, model_id: str) -> ModelStatus | None:
        model = self.config.models.get(model_id)
        if model is None:
            return None
        return self._check(model_id, model)

    def role_status(self, role: str) -> ModelStatus | None:
        resolved = self.config.model_for_role(role)
        if resolved is None:
            return None
        model_id, _ = resolved
        return self.check(model_id)

    def _check(self, model_id: str, model: ModelConfig) -> ModelStatus:
        if model.key_env and not os.environ.get(model.key_env):
            return ModelStatus(
                model_id,
                model.label,
                model.model,
                False,
                False,
                f"{model.key_env} is not set",
            )
        if model.health:
            try:
                response = httpx.get(model.health, timeout=self.timeout)
            except httpx.HTTPError:
                response = None
            if response is not None and response.status_code < 400:
                return ModelStatus(
                    model_id, model.label, model.model, True, False, "ready"
                )
            free = _free_vram_mib()
            if free is None:
                reason = "server down · GPU state unknown"
            elif free < model.startable_when_free_mib:
                reason = f"server down · GPU busy ({free:,} MiB free)"
            else:
                return ModelStatus(
                    model_id,
                    model.label,
                    model.model,
                    True,
                    True,
                    f"GPU free ({free:,} MiB) — starts on first use",
                )
            return ModelStatus(
                model_id, model.label, model.model, False, False, reason
            )
        if model.key_env:
            return ModelStatus(
                model_id, model.label, model.model, True, False, "API key present"
            )
        return ModelStatus(model_id, model.label, model.model, True, False, "configured")
