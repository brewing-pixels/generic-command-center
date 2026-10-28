from __future__ import annotations

from datetime import date

import httpx
import pytest

from command_center.errors import TaskStoreError, TaskStoreNotFound
from command_center.taskstore import TaskStoreClient


def test_state_and_patch() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/state":
            return httpx.Response(
                200,
                json={
                    "projects": [
                        {
                            "id": "p1",
                            "name": "Project",
                            "status": "ongoing",
                            "rev": 2,
                            "focus_order": 1,
                            "objectives": [],
                        }
                    ]
                },
            )
        if request.url.path == "/api/projects/p1":
            return httpx.Response(
                200,
                json={
                    "id": "p1",
                    "name": "Project",
                    "status": "ongoing",
                    "rev": 3,
                    "focus_order": 2,
                    "objectives": [],
                },
            )
        return httpx.Response(404)

    with TaskStoreClient(
        "http://test", transport=httpx.MockTransport(handler)
    ) as client:
        state = client.state()
        assert state.projects[0].rev == 2
        assert state.projects[0].focus_order == 1
        updated = client.patch_project("p1", {"focus_order": 2, "if_rev": 2})
        assert updated.rev == 3
        assert updated.focus_order == 2

    assert requests[1].method == "PATCH"
    assert b'"if_rev":2' in requests[1].content


def test_agenda_404_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with TaskStoreClient(
        "http://test", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.get_agenda(date(2026, 10, 27)) is None


def test_error_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/projects/missing":
            return httpx.Response(404, json={"detail": "missing"})
        if request.url.path.startswith("/api/agendas/"):
            return httpx.Response(404, json={"detail": "no agenda"})
        return httpx.Response(409, json={"detail": "stale", "current_rev": 4})

    with TaskStoreClient(
        "http://test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(TaskStoreError) as caught:
            client.patch_project("p1", {"if_rev": 3})
        assert caught.value.status_code == 409
        assert caught.value.payload == {"detail": "stale", "current_rev": 4}
        with pytest.raises(TaskStoreNotFound):
            client.get_project("missing")
        assert client.get_agenda(date(2026, 10, 27)) is None
