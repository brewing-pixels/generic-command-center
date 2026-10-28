from __future__ import annotations


class CommandCenterError(Exception):
    """Base for errors that the CLI should print without a traceback."""


class ConfigError(CommandCenterError):
    pass


class TaskStoreError(CommandCenterError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class TaskStoreNotFound(TaskStoreError):
    pass


class PlanError(CommandCenterError):
    pass


class SpecError(CommandCenterError):
    pass


class FocusError(CommandCenterError):
    pass


class AgendaError(CommandCenterError):
    pass


class RunError(CommandCenterError):
    pass


class ShapeError(CommandCenterError):
    pass
