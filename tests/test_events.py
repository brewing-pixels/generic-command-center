from __future__ import annotations

from command_center.events import EventBus


def test_bus_assigns_ids_and_replays_after_cursor() -> None:
    bus = EventBus(history=3)
    live = bus.subscribe()
    first = bus.publish("run.started", {"run_id": "r1"})
    second = bus.publish("run.finished", {"run_id": "r1"})
    third = bus.publish("run.merged", {"run_id": "r1"})

    assert [first["id"], second["id"], third["id"]] == [1, 2, 3]
    assert [live.get_nowait()["id"] for _ in range(3)] == [1, 2, 3]

    replay = bus.subscribe(after_id=1)
    assert [replay.get_nowait()["id"] for _ in range(2)] == [2, 3]
    assert replay.empty()


def test_bus_replay_buffer_is_bounded() -> None:
    bus = EventBus(history=2)
    for index in range(4):
        bus.publish("run.log", {"index": index})
    replay = bus.subscribe(after_id=0)
    assert [replay.get_nowait()["id"] for _ in range(2)] == [3, 4]
    assert replay.empty()
