# Daily Agenda — Contract

*Status: v2, 2026-10-26. Companion to `SPEC.md` §6. Wire-level detail for the
agenda object, its endpoints, and the lock.*

## 1. Ownership

| Concern | Owner |
|---|---|
| Draft content, generation, item CRUD | Task store |
| Focus list, max 3, focus tag writes | Command Center |
| Validation, lock/unlock, snapshot | Command Center |
| Dispatch from locked items | Command Center |
| Display of locked agendas | Task store, read-only |

The store never locks. Command Center never owns or directly edits item
content; it may only patch `status` and `run` after lock, plus the agenda root's
lock fields. The web daemon proxies the store's content endpoints for
same-origin browser access; those writes still belong to the store.

## 2. Storage

- `taskstore/agendas/YYYY-MM-DD.yaml`, one file per local date.
- Local system timezone; no UTC date shifting.
- `rev: int` on the root, incremented on every content change and on lock/unlock.
  It is **not** incremented by agenda item `status`/`run` execution updates; the
  snapshot token tracks content, not execution.
- Retention: 90 days, then fold into monthly archive files.
- Listing: `GET /api/agendas?recent=N` returns summaries, newest first.

## 3. Object schema

| Field | Type | Rules |
|---|---|---|
| `date` | date | required; matches the filename |
| `status` | `draft \| locked` | required |
| `rev` | int | required; starts at 0 |
| `focus` | list[project_id] | snapshot at generation; ≤3 |
| `capacity` | object | `work_items`, `chores`, `operator_min` |
| `items` | list[item] | non-empty when locking |
| `locked_at` | datetime \| null | set by CC |
| `unlock_reason` | string \| null | required on unlock |
| `generated_by` | `model \| fallback \| manual` | optional, audit |

### Item schema

| Field | Type | Rules |
|---|---|---|
| `id` | string | `a_01`, `a_02`… assigned by the store; stable until the draft is replaced |
| `kind` | `work \| manual` | required |
| `project` | project_id | required; exists |
| `objective` | objective_id | required; exists |
| `task` | task_id | required; exists |
| `reason` | string | one line, generated or operator-written |
| `estimate_min` | int ≥ 0 \| null | advisory |
| `due_time` | `HH:MM` \| null | manual items only |
| `order` | int | unique; rewritten to `0..N-1` on PUT |
| `status` | `planned \| running \| review \| done \| dropped` | required |
| `run` | run_id \| null | set by CC at claim |
| `note` | string \| null | operator note |

`blocked` is never stored. Clients derive it from eligibility and show the
reason code.

## 4. Status transitions

Agenda:

```
draft --lock(CC)--> locked
locked --unlock(CC, reason)--> draft
```

Work item:

```
planned --CC claim--> running
running --gate/policy pass + integration merge--> done
running --gate/policy failure--> review
review  --retry--> running
review  --operator drops--> dropped
planned --operator drops--> dropped
```

Manual item:

```
planned --operator--> done | dropped
```

Rules:

- `done` for a work item is written **after** the merge into `cc/integration`.
- A failed or blocked run leaves the item in `review`, never silently
  `planned`; a retry moves it back to `running`.
- A no-diff run is blocked for the operator, never auto-completed.
- `dropped` cancels a queued run; an in-flight run stops at the next checkpoint.
- Only CC writes `running`, `review`, `done`; the operator may write `dropped`.

## 5. Endpoints

### Task store

| Method | Path | Rules |
|---|---|---|
| GET | `/api/agendas/{date}` | 200 or 404 |
| GET | `/api/agendas?recent=N` | summaries |
| PUT | `/api/agendas/{date}` | create or replace a draft; `409` when locked; `if_rev` required when it exists; items without an `id` get the next `a_NN`, existing ids are preserved |
| POST | `/api/agendas/{date}/suggest` | body `{replace?: bool, model?: string}`; persists; see §6 |
| POST | `/api/agendas/{date}/items` | draft only; assigns the next `a_NN` |
| PATCH | `/api/agendas/{date}/items/{id}` | draft: any content field; locked: only `status`, `run` |
| DELETE | `/api/agendas/{date}/items/{id}` | draft only |

### Command Center

| Method | Path | Rules |
|---|---|---|
| GET | `/api/agendas/{date}` | proxy of the store read; for the web client |
| PUT | `/api/agendas/{date}` | proxy of the store draft write; for the web client |
| POST | `/api/agendas/{date}/suggest` | proxy of the store suggestion; for the web client |
| POST | `/api/agendas/{date}/quick-add` | body `{project, title, due_time?, reason?, if_rev?}`; creates a task and a manual item |
| POST | `/api/agendas/{date}/lock` | body `{if_rev}`; validates; returns the snapshot |
| POST | `/api/agendas/{date}/unlock` | body `{if_rev, reason}`; clears the snapshot |
| GET | `/api/agendas/{date}/snapshot` | current locked snapshot |

### Errors

```json
{ "detail": "agenda is locked", "code": "agenda_locked", "current_rev": 3 }
```

Lock validation failure:

```json
{
  "detail": "1 item cannot run",
  "code": "agenda_invalid",
  "items": [
    { "id": "a_01", "code": "no_gate", "detail": "task t_8d47b3c1 has no gate" }
  ]
}
```

## 6. Suggest

1. Read the focus tags, open tasks, due dates, and recent run history.
2. Choose work items from focus projects that pass the structural suggestion
   filter (`SPEC.md` §8). The full candidate check runs at lock.
3. Choose manual items from any project: open tasks with no resolvable gate,
   prioritising due dates and staleness.
4. Respect the work cap; soft-fill chores up to the chore cap.
5. Produce `reason`, `estimate_min`, and order. Assign `a_01…`.
6. Persist the draft and increment `rev`.

Rules:

- No draft → create, return 200.
- Draft exists and `replace=false` → `409 agenda_exists`.
- Draft exists and `replace=true` → replace, return 200.
- Locked → `409 agenda_locked` regardless of `replace`.
- Only existing tasks may be selected. Anything new becomes an expansion
  suggestion, never an invented row.
- If the model is unavailable or returns invalid output, run the deterministic
  fallback and set `generated_by: fallback`. The draft must still be produced.
- The 21:00 timer is a systemd user timer in the store that calls `suggest` with
  `replace=false`. On gateway start, a catch-up call runs if no draft exists for
  tomorrow. Manual re-suggest in the cockpit confirms first and uses
  `replace=true`.

## 7. Lock

Validation is all-or-nothing. On failure nothing is written.

1. Agenda is `draft`; `date >= today`.
2. `items` non-empty; unique task refs; no duplicate IDs.
3. `focus` ≤ 3, all projects exist and are not parked/archived.
4. Work items ≤ `capacity.work_items` (hard). Chores and operator minutes only
   warn.
5. Dependency graph over work items is acyclic; every dependency is either
   `done` or appears earlier in `order`.
6. Every work item passes **candidate** eligibility (`SPEC.md` §8).
7. Every manual item references an open task.

On success, in one write to the store with `if_rev`:

- `status = locked`, `locked_at = now`, `rev++`;
- Command Center stores `{date, rev, items, locked_at}` locally.

Idempotency: locking an already-locked agenda with the same `rev` returns 200
with the snapshot; a different `rev` returns `409 stale_rev`.

## 8. Unlock

- Body: `{if_rev, reason}`; reason is required and non-empty.
- Writes `status = draft`, `unlock_reason = reason`, `rev++`.
- Command Center drops the snapshot.
- In-flight runs continue; no new dispatch until re-lock.
- Logged in the run/event history with the operator token id.

## 9. Snapshot and dispatch

- The snapshot freezes content and order, not execution status. Item status is
  read live from the store.
- Dispatch requires: `status = locked`, agenda `rev` equals the snapshot,
  `date` is today, item is `planned`, and **dispatch** eligibility passes.
- A passing run auto-merges into `cc/integration`; the item becomes `done` after
  that merge. There is no per-task human approval. The operator's code gate is
  the release merge to master.
- A `rev` mismatch means the store file changed behind CC's back
  (`agenda_stale`); dispatch stops until the operator re-locks.
- Manual items are never dispatched; CC only renders, reminds, and completes
  them through the store.

## 10. Day boundary

- Runs in flight at midnight finish; they are not killed.
- Past agendas are never dispatchable, even if still `locked`.
- Unfinished items roll into the next draft as suggestions; the next agenda is
  never auto-locked.
- If no draft exists in the morning, the cockpit shows "suggest now".

## 11. Quick-add

- The cockpit calls `POST /api/agendas/{date}/quick-add` on the CC daemon with
  `{project, title, due_time?, reason?, if_rev?}`.
- CC creates a task in the project's `o_general` objective (otherwise `422`),
  then adds a manual item for it. It requires an existing draft agenda.
- A locked agenda or stale `if_rev` returns `409`. If the task is created but
  the item fails, the error carries the task id; the task is never deleted.
- This is the only **task creation** initiated by a Command Center client, and
  it still goes through the store's task endpoint. Agenda content edits made in
  the web are proxied store writes, not CC-owned state.

## 12. Example

```yaml
date: 2026-10-26
status: draft
rev: 0
focus: [example-app, household]
capacity: {work_items: 3, chores: 3, operator_min: 90}
items:
  - id: a_01
    kind: work
    project: command-center
    objective: o_general
    task: t_8d47b3c1
    reason: "unblocks the store sync contract"
    estimate_min: 45
    due_time: null
    order: 0
    status: planned
    run: null
    note: null
  - id: a_02
    kind: manual
    project: household
    objective: o_general
    task: t_0000000a
    reason: "PSU for the spare machine"
    estimate_min: 20
    due_time: "18:00"
    order: 1
    status: planned
    run: null
    note: null
locked_at: null
unlock_reason: null
generated_by: model
```
