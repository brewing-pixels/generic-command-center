from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from .config import Config
from .errors import CommandCenterError
from .token import ensure_token


class DaemonError(CommandCenterError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        payload: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class DaemonClient:
    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.BaseTransport | None = None,
    ):
        host = "127.0.0.1" if config.daemon.bind in ("0.0.0.0", "::") else config.daemon.bind
        self._client = httpx.Client(
            base_url=f"http://{host}:{config.daemon.port}",
            timeout=30.0,
            transport=transport,
            headers={
                "Authorization": f"Bearer {ensure_token(config.token_path)}"
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DaemonClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DaemonError(f"daemon unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"detail": response.text}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise DaemonError(
                f"{method} {path}: {detail or response.status_code}",
                response.status_code,
                payload if isinstance(payload, dict) else None,
            )
        return response.json() if response.content else None

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health")

    def settings(self) -> dict[str, Any]:
        return self._request("GET", "/api/settings")

    def projects(self) -> dict[str, Any]:
        return self._request("GET", "/api/projects")

    def shape(self, project: str) -> dict[str, Any]:
        return self._request("GET", f"/api/shapes/{project}")

    def shape_start(
        self,
        project: str,
        rewrite: bool = False,
        *,
        objective: str | None = None,
        task: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"project": project, "rewrite": rewrite}
        if objective:
            payload["objective"] = objective
        if task:
            payload["task"] = task
        if model:
            payload["model"] = model
        return self._request("POST", "/api/shapes", json=payload)

    def shape_model(self, project: str, model: str) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/api/shapes/{project}", json={"model": model}
        )

    def shape_jobs(self, project: str) -> list[dict[str, Any]]:
        data = self._request("GET", f"/api/shapes/{project}/jobs")
        return data.get("jobs", [])

    def shape_enqueue(
        self, project: str, kind: str, **payload: Any
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/shapes/{project}/jobs", json={"kind": kind, **payload}
        )

    def shape_check(self, project: str) -> dict[str, Any]:
        return self._request("POST", f"/api/shapes/{project}/check")

    def shape_materialize(self, project: str) -> dict[str, Any]:
        return self._request("POST", f"/api/shapes/{project}/materialize")

    def shape_approve(self, project: str) -> dict[str, Any]:
        return self._request("POST", f"/api/shapes/{project}/approve")

    def shape_cancel(self, project: str) -> dict[str, Any]:
        return self._request("POST", f"/api/shapes/{project}/cancel")

    def shape_diff(self, project: str) -> dict[str, Any]:
        return self._request("GET", f"/api/shapes/{project}/diff")

    def models(self, *, force: bool = False) -> dict[str, Any]:
        params = {"force": "true"} if force else None
        return self._request("GET", "/api/models", params=params)

    def tasks(self, *, project: str | None = None) -> dict[str, Any]:
        params = {"project": project} if project else None
        return self._request("GET", "/api/tasks", params=params)

    def agenda(self, day: date) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/api/agendas/{day.isoformat()}")
        except DaemonError as exc:
            if exc.status_code == 404:
                return None
            raise

    def suggest_agenda(
        self, day: date, *, replace: bool = False, model: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"replace": replace}
        if model is not None:
            payload["model"] = model
        return self._request(
            "POST", f"/api/agendas/{day.isoformat()}/suggest", json=payload
        )

    def put_agenda(self, day: date, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PUT", f"/api/agendas/{day.isoformat()}", json=payload
        )

    def quick_add(
        self, day: date, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/agendas/{day.isoformat()}/quick-add", json=payload
        )

    def focus(self) -> dict[str, Any]:
        return self._request("GET", "/api/focus")

    def set_focus(self, project_ids: list[str]) -> dict[str, Any]:
        return self._request("PUT", "/api/focus", json={"projects": project_ids})

    def lock_agenda(
        self, day: date, if_rev: int | None = None
    ) -> dict[str, Any]:
        kwargs = {"json": {"if_rev": if_rev}} if if_rev is not None else {}
        return self._request(
            "POST", f"/api/agendas/{day.isoformat()}/lock", **kwargs
        )

    def unlock_agenda(self, day: date, reason: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/agendas/{day.isoformat()}/unlock",
            json={"reason": reason},
        )

    def snapshot(self, day: date) -> dict[str, Any] | None:
        return self._request("GET", f"/api/agendas/{day.isoformat()}/snapshot")

    def start_run(
        self,
        task_id: str,
        model_id: str | None = None,
        reviewer_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"task": task_id}
        if model_id:
            payload["model"] = model_id
        if reviewer_id:
            payload["reviewer"] = reviewer_id
        return self._request("POST", "/api/runs", json=payload)

    def runs(
        self, *, project: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        params = {
            key: value
            for key, value in {"project": project, "state": state}.items()
            if value
        }
        return self._request("GET", "/api/runs", params=params)

    def run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/runs/{run_id}")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request("POST", f"/api/runs/{run_id}/cancel")

    def logs(
        self, run_id: str, *, stream: str = "agent", offset: int = 0
    ) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/runs/{run_id}/logs",
            params={"stream": stream, "offset": offset},
        )

    def artifact(self, run_id: str, kind: str) -> str:
        path = f"/api/runs/{run_id}/artifacts/{kind}"
        try:
            response = self._client.get(path)
        except httpx.HTTPError as exc:
            raise DaemonError(f"daemon unreachable: {exc}") from exc
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = {"detail": response.text}
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise DaemonError(
                f"GET {path}: {detail or response.status_code}",
                response.status_code,
                payload if isinstance(payload, dict) else None,
            )
        return response.text

