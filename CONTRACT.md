# Wire Contracts

*Status: v2, 2026-10-26. Companion to `SPEC.md`, `AGENDA.md`, and `SHAPE.md`.
Exact shapes that both sides implement against.*

## 1. Store revisions (`rev` / `if_rev`)

- `rev: int` exists on **project**, **objective**, **task**, and the **agenda
  root**. New entities start at `0`.
- Every content write increments `rev` exactly once through one `_touch()`
  helper. A write path that forgets it is a bug.
- UI-only writes — `collapsed`, board order, task reorder — do **not** bump
  `rev` and do not invalidate a claim.
- Agenda item `status`/`run` execution updates do **not** bump the agenda root
  `rev`; the snapshot token tracks content, not execution.
- `updated` remains display-only. It is never a conflict token.
- `if_rev` is an optional integer field on PATCH/PUT bodies. Command Center
  always sends it; a store UI may omit it (last-write-wins for human edits).
- Stale precondition:

```json
{ "detail": "revision mismatch", "code": "stale_rev", "current_rev": 7 }
```

HTTP `409`. The client refetches, re-applies its intent, and retries once.
- Legacy YAML without `rev` parses as `0`; the next write persists it.
- Every API response that returns the entity includes its `rev`.
- Test rule: a table-driven test hits every mutating endpoint and asserts `rev`
  increased by exactly one.

## 2. Focus tag

- Field on a project: `focus_order: 1 | 2 | 3 | null`.
- Display only. The store renders it read-only; it has no focus endpoint.
- Command Center is the only writer. `PUT /api/focus` with an ordered list of at
  most three project ids is applied in one transaction; CC then PATCHes the tags
  with `if_rev`.
- A failed tag write is retried on the next sync; CC's list remains the source
  of truth.
- Parking or archiving a project clears its tag and removes it from the list.
- The gateway does not enforce a maximum. The invariant is CC's.

## 3. Spec state and digest

```yaml
spec_state:
  docs: [docs/spec/SPEC.md, docs/spec/DESIGN.md]
  digest: sha256:9f2c…
  approved_at: 2026-10-26T18:04:11Z
  approved_in_commit: 4b1e0c2…
  dirty: false
  phase: approved        # shape | approved | building | done
```

- `spec_path` remains the viewer pointer. `docs` is the digest set.
- `SPEC.md` is required; `DESIGN.md` is optional. Defaults are
  `docs/spec/SPEC.md` and `docs/spec/DESIGN.md`; a project may override with
  `cc spec approve <project> --docs A.md,B.md`.
- `command-center.yaml` is **not** part of the digest.

Digest algorithm — committed blobs only, never the working tree:

```
for path in sort(docs):
    blob = git show <commit>:<path>
    line = path + "\0" + sha256(blob) + "\n"
digest = "sha256:" + sha256(concat(lines))
```

Approval:

1. `cc spec approve <project>` reads the docs from `cc/integration` HEAD. Before
   that branch exists (P1), it uses the repository HEAD; P2 bootstraps
   integration from master, so the approved commit carries over. If master has
   advanced past integration, it fast-forwards integration from master first.
2. It refuses if any doc is missing, untracked, or dirty; it never approves a
   working-tree-only spec.
3. It writes `docs`, `digest`, `approved_at`, `approved_in_commit`, `dirty:
   false`, `phase: approved` to the store with `if_rev`.
4. A Shape run does the same automatically when its diff is approved.

Dispatch check:

- `spec_state` exists and `phase` is `approved` or `building`;
- digest recomputed from `cc/integration` HEAD equals `spec_state.digest`.

Failure codes: `spec_unapproved` (missing), `spec_dirty` (mismatch). A direct spec PUT
still writes, but returns
`"warning": "editing this invalidates spec approval"` when the file is in an
approved digest set; `dirty` is not recomputed by the gateway.

`phase` moves to `building` when the first run starts and `done` when the
project objective closes. Command Center sets `dirty: true` when it detects a
digest mismatch at dispatch; re-approval clears it.

## 4. Eligibility reason codes

Stable codes. Every check returns `{code, detail}`; `cc status --json` and the
cockpit use the same codes.

| Code | Stage | Meaning |
|---|---|---|
| `project_missing` | candidate | path missing on disk |
| `not_git` | candidate | project path is not a git repository |
| `project_parked` | candidate | project status is `parked` |
| `project_archived` | candidate | project status is `archived` |
| `objective_closed` | candidate | objective is `done` or `dropped` |
| `task_not_open` | candidate | task is not `open` |
| `no_gate` | candidate | no `command-center.yaml` entry or empty gate |
| `dep_missing` | candidate / lock / dispatch | a dependency is not done |
| `dep_later` | lock | a dependency appears after the dependent |
| `cycle` | lock | dependency cycle in the agenda |
| `duplicate_task` | lock | the same task appears twice on the agenda |
| `spec_unapproved` | candidate | no `spec_state` |
| `spec_dirty` | candidate | digest mismatch at the base ref |
| `base_dirty` | candidate | operator checkout has uncommitted, non-ignored files |
| `task_locked` | candidate | a live lock exists for the task |
| `agenda_not_locked` | dispatch | no locked agenda for today |
| `not_on_agenda` | dispatch | task is absent from today's locked agenda |
| `agenda_stale` | dispatch | agenda rev differs from the snapshot |
| `project_busy` | dispatch | another run is active for the project |
| `model_down` | candidate warning / dispatch | the resolved model is unavailable |
| `over_capacity` | lock | work-item cap exceeded |
| `agenda_empty` | lock | agenda has no items |
| `agenda_past` | lock | agenda date is before today |
| `focus_invalid` | lock | focus has more than three or duplicate project ids |
| `agenda_locked` | lock/suggest | agenda is already locked |
| `stale_rev` | lock/PATCH | `if_rev` mismatch |
| `spec_check_failed` | shape | `cc spec check` failed |
| `gate_dirty` | gate | the gate modified tracked or non-ignored files |
| `policy_secret` | policy | secrets or private keys in the diff |
| `policy_env` | policy | `.env*` file in the diff |
| `policy_binary` | policy | binary file in the diff |
| `policy_scope` | policy | changed file outside the task's scope |
| `policy_spec` | policy | `docs/spec/**` or `command-center.yaml` changed |
| `policy_size` | policy | diff exceeds the file or byte cap |

Model availability is a warning at candidate stage and a hard `model_down` at
dispatch.

## 5. Command Center HTTP API

- Base: `http://127.0.0.1:4190`, bind configurable to `0.0.0.0`.
- Machine clients: `Authorization: Bearer <token>`, token at
  `~/.config/command-center/token`.
- Browser: `POST /api/auth/pin` with the configured LAN PIN; the daemon returns
  an in-memory session token. Browser requests and SSE use that session token.
  Missing or wrong credentials → `401`; the PIN endpoint rate-limits failures.
- JSON everywhere; errors use `{detail, code, current_rev?}`.
- Draft agenda content is owned by the task store. The daemon proxies
  `GET/PUT /api/agendas/{date}` and `POST /api/agendas/{date}/suggest` so the
  same-origin web app needs only one base URL and one token. The CC API owns
  lock/unlock and the snapshot; it does not become a second content owner.
- The task store is assumed unauthenticated. Keep it on the trusted LAN;
  do not expose it publicly.

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | web shell; public, no data without a token |
| GET | `/static/*` | web assets |
| POST | `/api/auth/pin` | `{pin}`; exchanges the LAN PIN for a session token |
| GET | `/api/status` | daemon, agenda, queue, model summary |
| GET | `/api/projects` | project ids, names, status, focus order, spec phase |
| GET | `/api/settings` | read-only daemon/store/path/role/budget/policy view; no secrets |
| POST | `/api/shapes` | `{project, rewrite?, objective?, task?}`; prepare worktree and start the inquisitor |
| GET | `/api/shapes/{project}` | latest shape, questions, and last check |
| POST | `/api/shapes/{project}/answer` | `{question_id, answer}` |
| POST | `/api/shapes/{project}/revise` | resume the inquisitor with answers; requires all questions answered |
| POST | `/api/shapes/{project}/check` | pristine check on the active shape |
| POST | `/api/shapes/{project}/materialize` | create store ids + `command-center.yaml` |
| POST | `/api/shapes/{project}/approve` | merge to integration and approve the spec |
| POST | `/api/shapes/{project}/cancel` | remove worktree, restore previous spec state |
| GET | `/api/shapes/{project}/diff` | shape batch diff |
| GET | `/api/tasks?project=&reason=` | queue with eligibility reasons and `objective_title` |
| GET | `/api/focus` | ordered focus list |
| PUT | `/api/focus` | `{projects: [id, …]}`; max 3, atomic; writes tags |
| GET | `/api/models?force=` | model availability |
| GET | `/api/agendas/{date}` | proxy of the store's agenda; 404 when absent |
| PUT | `/api/agendas/{date}` | proxy of the store draft write; requires `if_rev` on an existing draft |
| POST | `/api/agendas/{date}/suggest` | proxy `{replace?, model?, if_rev?}` |
| POST | `/api/agendas/{date}/quick-add` | `{project, title, due_time?, reason?, if_rev?}`; creates task + manual item |
| POST | `/api/agendas/{date}/lock` | `{if_rev}`; validates, snapshots |
| POST | `/api/agendas/{date}/unlock` | `{if_rev, reason}` |
| GET | `/api/agendas/{date}/snapshot` | locked snapshot |
| POST | `/api/runs` | `{task, model?}`; claim and dispatch; returns run id |
| GET | `/api/runs?state=&project=` | run list |
| GET | `/api/runs/{id}` | run detail + artifacts |
| GET | `/api/runs/{id}/artifacts/{kind}` | one artifact: `diff`, `gate`, `policy`, `review`, `run`, `prompt`, `agent`, `stderr`, `changes` |
| GET | `/api/runs/{id}/logs?stream=agent\|stderr&offset=` | incremental log tail `{offset, text, eof, state, attempt}` |
| POST | `/api/runs/{id}/cancel` | stop at next checkpoint |
| GET | `/api/events` | SSE stream; `?token=<token>` is accepted for `EventSource` |

## 6. SSE events

`GET /api/events` emits named events with an `id:` line and a JSON `data:`
payload. A reconnect may send `Last-Event-ID` (or `last_event_id` query); the
daemon replays up to the last 100 in-memory events after that cursor. A daemon
restart resets the ids and buffer; clients re-poll current state on reconnect.

| Event | Payload |
|---|---|
| `run.started` | `{run_id, project, task, model}` |
| `run.finished` | `{run_id, state, verdict}` |
| `gate.failed` | `{run_id, command, exit_code}` |
| `policy.failed` | `{run_id, codes}` |
| `run.merged` | `{run_id, merge_sha}` |
| `run.blocked` | `{run_id, code, detail}` |
| `review.verdict` | `{run_id, verdict}` |
| `agenda.locked` | `{date, rev}` |
| `agenda.unlocked` | `{date, rev, reason}` |
| `model.down` | `{id, reason}` |
| `shape.updated` | `{shape_id, project, state}` |

Clients resubscribe with `Last-Event-ID`; the daemon replays the last 100 events.

## 7. Run record (`state/runs/<id>/run.json`)

| Field | Meaning |
|---|---|
| `id` | `r_<hex>` |
| `kind` | `shape` or `build` |
| `project`, `objective`, `task`, `agenda_item` | refs |
| `attempt` | 1-based |
| `model_requested`, `model_resolved`, `provider` | exact coder ids |
| `reviewer_model`, `reviewer_resolved` | reviewer role id and exact CLI model |
| `reviewer_session_id` | fresh reviewer session |
| `reviewer_verdict`, `reviewer_path` | final verdict and `review.json` |
| `base_sha` | `cc/integration` at dispatch |
| `branch`, `worktree` | paths |
| `session_id` | OpenCode session |
| `state` | same enum as the spec |
| `gate_path`, `diff_path`, `policy_path` | artifacts |
| `policy_passed` | boolean or null |
| `merge_sha` | integration commit after a passing merge |
| `verdict` | `merged`, `blocked`, `cancelled`, or a run error code |
| `started_at`, `finished_at` | UTC |

### Status mapping

| Run outcome | store task | Agenda item |
|---|---|---|
| queued / running | `doing` | `running` |
| gate/policy/reviewer changes, retrying | `doing` | `review` |
| merged into integration | `done` | `done` |
| reviewer block | `blocked` | `review` |
| no diff | `blocked` | `review` |
| cancelled | `open` | `planned` |
| error / exhausted | `blocked` | `review` |

`blocked` stops re-dispatch until the operator unblocks. `done` is written only
after the integration merge. Shipping integration to master is not a task
status and not a Command Center action.

## 8. Gate result (`gate.json`)

```json
{
  "passed": false,
  "commands": [
    {
      "argv": ["uv", "run", "pytest", "-q"],
      "exit_code": 1,
      "duration_ms": 8421,
      "tail": "…last 8 KiB…"
    }
  ],
  "started_at": "…",
  "finished_at": "…"
}
```

- Fail fast: commands after the first non-zero exit are omitted.
- Environment: `CI=1`, `NO_COLOR=1`, provider credentials stripped.
- Default timeout: 900s per command, overridable in `command-center.yaml`.

### Policy result (`policy.json`)

```json
{
  "passed": false,
  "files": 3,
  "bytes": 4210,
  "findings": [
    {
      "code": "policy_secret",
      "path": "src/config.py",
      "detail": "possible API key in an added line"
    }
  ]
}
```

- The scan runs on the staged diff (`git add -A`, `git diff --cached base`).
- Checks: secrets/private keys in added lines, `.env*`, binaries, changes to
  `docs/spec/**` or `command-center.yaml`, files outside the task's optional
  `scope`, and the configured file/byte caps.
- Findings carry the `policy_*` codes from §4. Any finding blocks the merge.
- Scope globs use `pathlib.PurePath.match`; a file must match at least one.

## 9. Reviewer output

The reviewer's final message must be one JSON object:

```json
{
  "verdict": "approve",
  "reasons": ["implements the task; gate passes"],
  "findings": [
    {"file": "src/x.py", "line": 12, "severity": "warn", "note": "…"}
  ]
}
```

- `verdict`: `approve`, `changes`, or `block`.
- `reasons`: required, 1–5 short strings.
- `findings`: optional; `severity` is `block`, `warn`, or `note`.
- Any parse error, missing field, unknown verdict, or non-JSON output is treated
  as `changes`. Never as approval.
- Style findings are `warn`/`note`; `block` is reserved for correctness, scope,
  security, and spec drift.
- `approve` continues to the integration merge. `changes` consumes one attempt
  from the same `max_attempts` budget as the coder and retries. `block` stops
  immediately; no further attempt is spent.
- The reviewer always runs in a fresh session. Its model is `roles.reviewer`
  and may match the coder's model. `cc run` refuses at create when they match; there is no manual
  override.

## 10. OpenCode adapter

```
opencode run --standalone --agent <id> --model <provider/model> \
  --format json --auto "<prompt>"
```

- `cwd`: the task worktree. Chat sessions use the project repo or CC repo.
- Set `PWD` to the resolved worktree in the child environment; `subprocess.cwd`
  alone leaves the OpenCode shell in the operator's launch directory.
- `--format json` is line-delimited event output; stream stdout/stderr to
  `agent.jsonl` / `agent.stderr.log` as lines arrive. The reviewer appends to
  the same files after the coder. Extract the session id and the final assistant
  text from the coder's/reviewer's own output. If the event shape is
  unrecognized, fail the run loudly rather than guessing.
- `--auto` is allowed only because the deny list is explicit. No `ask` rules may
  exist in a headless agent.
- Kill the whole process group on timeout or cancellation.
- Agent source lives in the CC repo `agents/`; install to
  `~/.config/opencode/agents/` with `cc agents install`.
- The four agent files are `cc-inquisitor`, `cc-coder`, `cc-reviewer`, and
  `cc-chat`; P4 installs `cc-chat` without a chat UI. P5 uses `cc-inquisitor`
  for the shape loop; the full schema is in `SHAPE.md`.
- The agent files grant read/edit access under the default run directory
  `~/.local/state/command-center/runs/**`. If `state_dir` moves, update
  `agents/*.md` and reinstall.
- Refuse to run if the target repo defines any `cc-*` agent.
- The reviewer model is configured; it may match the coder's model (recommended
  not to). The run does not refuse at create
  when they match; there is no override.
- On timeout kill the whole process group; logs already written remain.

## 11. Model probe (`GET /api/models`)

```json
{
  "models": [
    {
      "id": "hosted",
      "label": "Hosted model",
      "model": "provider/model-name",
      "up": true,
      "startable": false,
      "reason": "API key present"
    }
  ]
}
```

- A model with `key_env`: the named variable being present means up; there is
  no health endpoint.
- A model with `health`: `GET` it; when down, check free VRAM; if at least
  `startable_when_free_mib`, `startable: true`. Start it through the configured
  `local.start_script` under the same lock so two callers do not race.
- Cache 15s; `force=1` bypasses.
- No silent fallback. A down model fails the operation with its reason.

## 12. Shape artifacts

| File | Required | Purpose |
|---|---|---|
| `docs/spec/SPEC.md` | yes | requirements, acceptance, scope |
| `docs/spec/DESIGN.md` | no | technical design |
| `docs/spec/TASKS.yaml` | for a Shape run | decomposition; the source for store tasks and gates |

`TASKS.yaml`:

```yaml
objectives:
  - title: "store contract"
    acceptance:
      - "every mutating endpoint increments rev"
    gate:
      - ["uv", "run", "pytest", "-q", "tests/test_rev.py"]
    tasks:
      - slug: rev-endpoints
        title: "Add rev and if_rev"
        acceptance:
          - "stale writes return 409"
        gate:
          - ["uv", "run", "pytest", "-q", "tests/test_rev.py"]
        depends_on: []
        scope:
          - "store/**"
          - "tests/test_rev.py"
      - slug: agenda-endpoints
        title: "Add agenda endpoints"
        gate:
          - ["uv", "run", "pytest", "-q", "tests/test_agenda.py"]
        depends_on: [rev-endpoints]
```

- Slugs are local to the file. Command Center creates the store tasks, gets the
  real ids, and writes `command-center.yaml` keyed by those ids.
- `cc spec check` validates: no `TBD`/`TODO`, every objective has acceptance and
  a gate, every task has a gate and dependencies, no cycles, every gate is a
  non-empty argv list, every optional `scope` entry is a non-empty glob, and the
  spec docs and `TASKS.yaml` are committed.
- The digest covers `SPEC.md` + `DESIGN.md` only. `TASKS.yaml` and
  `command-center.yaml` are reviewed in the Shape diff but are not digest-bound.

## 13. Config skeleton

```yaml
# config.yaml — Command Center repo, versioned
daemon:
  bind: 127.0.0.1
  port: 4190
  pin: "0000"              # browser login; 4–8 digits, trusted LAN only
taskstore:
  url: http://127.0.0.1:4180
  poll_seconds: 30
state_dir: ~/.local/state/command-center
worktree_root: ~/.local/state/command-center/worktrees
token_path: ~/.config/command-center/token
opencode:
  binary: opencode
  agents_dir: ~/.config/opencode/agents
  chat_server: null        # reserved; chat is not implemented
local:
  start_script: /path/to/local-model-start.sh
budgets:
  max_attempts: 3
  gate_timeout_seconds: 900
policy:
  max_files: 200
  max_diff_bytes: 1000000
```

- Merge the `models` and `roles` block from `SPEC.md` §5 into this file.
- `bind: 0.0.0.0` requires the bearer token and is LAN-only.
- `state_dir` holds SQLite, agenda snapshots, run artifacts (including
  `review.json`), and worktrees.
- The web shell is public; every `/api/*` route except `/api/health` requires
  the bearer token. The browser stores the token locally and uses
  `?token=<token>` only for the `EventSource` connection.
- `chat_server: null` is reserved for the deferred chat surface; it is unused.
