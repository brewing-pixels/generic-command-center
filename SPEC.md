# Command Center — Specification

**Status:** locked. This document describes the engine as built; deviations are
bugs or a new decision, not drift.

Related contracts: [`CONTRACT.md`](CONTRACT.md) (wire formats),
[`AGENDA.md`](AGENDA.md) (the daily agenda), [`SHAPE.md`](SHAPE.md) (the shape
loop), [`SECURITY.md`](SECURITY.md) (threat model).

## 1. What this is

A local-first execution layer. It takes declared work from a **task store**,
commits a small daily agenda, grinds a project's specification until it is
checkable, decomposes it into objectives and tasks, then implements and verifies
them until the objective's gate passes.

Two horizons, one loop:

1. **Day — commit.** Pick up to three focus projects, review the suggested
   agenda, lock what will actually run today.
2. **Project — shape and build.** Argue the spec to a close, then dispatch one
   task at a time into isolation, verify it with a gate and a policy scan the
   writer did not write, and let it land in integration.

**Not a task manager.** The task store owns the plan and the CRUD of projects,
objectives, tasks, expansions, focus tags and agenda content. Command Center owns
execution: locks, worktrees, runs, gates, approvals, audit, the focus list, the
agenda lock and agent files. It reads the store and writes back only status,
execution state, focus tags and the agenda lock.

**One engine, several clients.** A daemon owns all state and exposes one
versioned API. The CLI, the web UI and the native cockpit are clients; anything
else that speaks the API is a client too.

## 2. Locked decisions

| Decision | Choice |
|---|---|
| Spec ownership | **The project repo.** `docs/spec/SPEC.md` + `DESIGN.md` are canonical, versioned with the code. The store mirrors digest, approval and phase; it never owns the content. |
| Spec adoption | **Audit, not rewrite.** Existing docs are inventoried; the inquisitor asks only about gaps. A from-scratch rewrite is opt-in. |
| Autonomy v1 | **L2 task landing.** Gate + policy + reviewer auto-merge a task into `cc/integration`. Moving that branch to master is a plain git merge outside the tool; the machine never pushes. Spec and design stay human-approved. |
| Dogfood target | **None fixed.** The first project is chosen at runtime from the store board. |
| Agents | **Three pipeline agents** (inquisitor, coder, reviewer) **+ one interface agent** (`cc-chat`). Verification and policy are code, not agents. |
| Gates | **Declared in the project repo** (`command-center.yaml`), versioned with the code they validate. The store carries no execution fields. |
| Git | **Never push.** Runs commit on a branch, then merge into `cc/integration`; you merge integration to master. |
| Scope discovered mid-run | Becomes a **store expansion** (`raw`), never a silent code change. |
| Verification | **Deterministic.** Command Center runs the gate itself, from the approved gate file. An agent's self-report is not evidence. |
| Claim token | **`rev` / `if_rev`.** Every content write increments an integer revision; conflicts are checked on the revision, never on a timestamp. |
| Focus | **Maximum three projects.** The store holds a read-only `focus_order` tag for display; Command Center holds the list, enforces the invariant and is the only writer. |
| Agenda | **A store object per day**, `draft → locked`. The store generates drafts; Command Center validates, locks and snapshots. Only locked items dispatch. |
| Queue | **The task store is the queue.** SQLite holds locks, runs, events, focus and a frozen task snapshot taken at claim. |
| Landing | **`cc/integration`.** Tasks auto-merge on gate + policy + reviewer. You review the delta and merge to master. |
| Spec approval | **Digest of the declared spec docs** (SPEC + DESIGN), computed from committed blobs. Gates are excluded. `cc spec approve` covers pre-shaped projects. |
| Surfaces | **One daemon + versioned HTTP API.** No client holds state or business logic. |
| Models | **Allowlist** configured explicitly. Role defaults in config plus a per-operation override recorded on the run. No silent fallback. |
| LAN access | **Bearer token for machine clients, PIN exchange for the browser.** Localhost by default; LAN binding is explicit opt-in. |

## 3. Boundary

| System | Role | Owns |
|---|---|---|
| **Task store** (external) | what should be done | projects, objectives, tasks, expansions, focus tags, agendas, spec pointer and digest |
| **Command Center** | doing it | queue consumption, locks, worktrees, runs, gates, approvals, audit, focus list, agent files |
| **Clients** | operating it | CLI, web, native cockpit |

Rules:

1. The store is the source of truth for *what* and for final status.
2. Command Center is the source of truth for *how* and for run history.
3. Pull by project/objective/task id; never duplicate work already in a run.
4. Pushing status is idempotent and conflict-checked on `rev`.
5. A dropped or archived task cancels its queued run; an in-flight run stops at
   the next checkpoint. A completed run is never silently overwritten.
6. Focus and agendas are store objects. The store owns their content; Command
   Center owns the focus list, the agenda lock and the snapshot. Command Center
   may transition an agenda `draft → locked`, record run ids and item status,
   and mark manual items done. It never edits an item's content.
7. A target repository cannot configure the executor: no models, no agent
   permissions, no mid-run gate changes. The code being executed must not
   control the code that judges it.
8. Chat is read-only. Its only durable output is an expansion draft you
   explicitly file.
9. Verified work lands on `cc/integration` automatically. Command Center stops
   there: moving integration to master happens outside the tool.

## 4. Agents — three pipeline, one interface

| Agent | Phase | Job | Repo access | Output contract |
|---|---|---|---|---|
| **inquisitor** | Shape | Read the repo and brief, adopt or propose spec + design, surface open questions with proposed defaults, produce the task decomposition | read-only outside `docs/spec/*` | `SPEC.md`, `DESIGN.md`, `TASKS.yaml`, question queue |
| **coder** | Build | Implement one task in one worktree | read/write in worktree and run dir | diff + summary |
| **reviewer** | Build | Independent review of the diff against the task, the spec digest and the gate output | read-only | JSON verdict: `approve` / `changes` / `block`, with reasons |
| **cc-chat** | none | Brainstorm, explain state, scope an idea before it becomes an expansion | read-only | prose + optional expansion draft |

`cc-chat` is an interface, not a stage. The pipeline stays at three agents.

Deterministic code replaces roles that would otherwise be agents:

- **Tester** → the gate runner: executes declared commands, records exit codes.
- **Guardian** → the policy scan: secrets, `.env`, out-of-scope paths, forbidden
  git operations, spec and gate immutability, diff size limits.
- **Tech-writer** → the coder with a docs gate.
- **Analyst** → the inquisitor's first prompt.

### 4.1 OpenCode mapping

The engine uses four custom agents with fixed permissions and output contracts,
and role-default models that a per-run picker may override. Their source of truth
lives in this repo under `agents/` and is installed to
`~/.config/opencode/agents/`. The system directory has the highest configuration
precedence, so a target repository cannot shadow them; as a second belt, the
engine refuses to run when the target repo defines any `cc-*` agent.

| Agent id | Writes | Shell | Network |
|---|---|---|---|
| `cc-inquisitor` | `docs/spec/**` | deny | deny |
| `cc-coder` | worktree + run dir | allow, except `git push/commit/remote/config`, `gh`, `sudo` | deny web tools |
| `cc-reviewer` | nothing | deny | deny |
| `cc-chat` | nothing | only `cc status*`, `cc runs*`, `cc run show*`, `cc agenda show*` | deny |

Invocation rules:

- `opencode run --agent <id> --model <provider/model> --format json --auto`.
  The model is always passed explicitly; a session keeps its own model separate
  from the agent's.
- No `ask` permission rules in headless runs — they block forever. Dangerous
  actions are denied explicitly; `--auto` covers only what is safe.
- The coder's permissions deny `git commit` and `git push`, not just its prompt.
- The inquisitor cannot ask interactive questions. It emits a numbered batch with
  proposed defaults as text.
- The reviewer runs in a fresh session, receives `diff.patch`, the task, the spec
  digest, `gate.json` and `policy.json`, and must return valid JSON. Invalid JSON
  is a `changes` verdict, never approval.
- Reviewer and coder share one budget: `max_attempts` covers coder → gate →
  policy → reviewer. `changes` consumes the attempt; `approve` lands; `block`
  stops immediately.
- The coder leaves the worktree uncommitted. The harness commits.
- Pipeline runs are one-shot and isolated. Chat sessions attach to a long-lived
  server so a turn does not pay process startup each time.

## 5. Models and providers

A curated allowlist, not every provider the harness can see.

```yaml
# config.yaml — versioned template: config.example.yaml
models:
  hosted:
    label: Hosted model
    model: provider/model-name
    key_env: PROVIDER_API_KEY
    context_window: 1000000
  local:
    label: Local model
    model: local/model-name
    health: http://127.0.0.1:8080/health
    startable_when_free_mib: 15000
    context_window: 262144

roles:
  inquisitor: hosted
  coder: hosted
  reviewer: hosted
  chat: hosted
```

`cc models` and `GET /api/models` return
`{id, label, model, up, startable, reason}`. A hosted model is probed by key
presence; a local one by `/health` and free VRAM. Status is cached briefly and
forced when a picker opens.

Rules:

- **Role defaults plus per-operation override.** A picker choice applies to one
  run or one shaping session, is recorded on the artifact, and never mutates the
  role default.
- **No silent fallback.** If the chosen model is down, the operation fails with
  the reason. Quietly moving a local run to a hosted one would ship private code
  without consent.
- **The reviewer is configured, not assumed.** `roles.reviewer` names it and it
  may match the coder's model. A different model is recommended for independent
  review, but the harness does not refuse the run. The reviewer session is
  always fresh.
- **One model per attempt.** Retries record their own `model_requested` and
  `model_resolved`; escalation is deliberate.
- **Projects cannot choose models.** `command-center.yaml` carries no model
  field.
- **A local model gets one slot.** A single GPU-resident model cannot serve
  parallel runs; resolution holds a per-provider semaphore. Starting it shares
  the store's start script and lock so the two do not race.
- **Record the exact CLI string.** `run.json` stores the model passed to the
  agent runner, so a run is reproducible.

The picker appears as a segmented control on run launch and as `cc run --model`
in the CLI. Agenda auto-dispatch uses role defaults only.

## 6. Focus and the daily agenda

### 6.1 Focus

- The store keeps `focus_order: 1|2|3|null` on a project. It is a display tag:
  the store renders the badge and does not allow editing it.
- Command Center holds the ordered focus list in SQLite, enforces the maximum of
  three in one transaction, and is the only writer of the tag.
- A swap is remove-then-add in one transaction. The tag is reconciled to the
  list afterwards; a failed tag write is retried on the next sync.
- Persistent until you change it. After seven unchanged days the UI shows a
  stale chip; it never auto-clears.
- Parking or archiving a focused project removes it and clears the tag.
- Focus drives **suggestions**, not dispatch. A task outside focus can still be
  added to an agenda by hand.
- The suggester reads the tags. A one-sync lag is acceptable; it never calls
  Command Center.

### 6.2 Agenda object

One file per day in the store, `agendas/YYYY-MM-DD.yaml`. The full wire contract
is in `AGENDA.md`; the shape is:

```yaml
date: 2026-10-26
status: draft          # draft | locked
rev: 0
focus: [example-app, household]
capacity:
  work_items: 3
  chores: 3
  operator_min: 90
items:
  - id: a_01
    kind: work         # work | manual
    project: example-app
    objective: o_general
    task: t_00000001
    reason: "unblocks the store sync contract"
    estimate_min: 45
    order: 0
    status: planned    # planned | running | review | done | dropped
    run: null
  - id: a_02
    kind: manual
    project: household
    objective: o_general
    task: t_00000002
    due_time: "18:00"
    status: planned
locked_at: null
unlock_reason: null
```

Rules:

- Every item references an existing store task. There is no second task store.
- `manual` items carry no gate and are verified by the human. Command Center
  renders them, reminds, and marks them done through the store.
- Work items must pass **candidate eligibility** (§8). Lock validates the whole
  set; one invalid work item rejects the lock with per-item reasons.
- Locking is a Command Center action: it validates, writes `status: locked` and
  `locked_at` with `if_rev`, and stores a local snapshot of item content and
  order plus the agenda rev. Execution status is read live, never from the
  snapshot.
- Locked content is immutable. Only `status` and `run` on an item may change;
  everything else requires an unlock. Unlock requires a reason, is logged, and
  does not kill an in-flight run.
- If the store's agenda rev differs from the snapshot, dispatch stops until the
  agenda is re-locked (`agenda_stale`).
- `blocked` is derived by clients from eligibility; it is never stored.
- Runs in flight finish at midnight. Unfinished items roll into the next draft,
  never auto-locked. Past agendas are never dispatchable.

### 6.3 Generation

- A scheduler in the store generates the draft at 21:00 local by default and on
  demand from the cockpit or web UI. The 21:00 run uses `replace=false`; a
  gateway or client start performs a catch-up call when no draft exists.
- `suggest` persists. No draft → create. Draft exists → `409` unless
  `replace=true`. Locked → `409`.
- The suggester may only select existing store tasks. New work becomes an
  expansion, not an invented agenda row.
- A deterministic fallback (eligible by priority, dependencies done, due date,
  staleness) produces a usable draft if the model is down. The model improves
  ordering and reasons; it is not the only reason a morning exists.
- If no draft exists by morning, the cockpit offers "suggest now".

### 6.4 Capacity and guardrails

- Defaults: three work items, three chores, `operator_min: 90`. Tune with real
  data.
- The work-item cap is **hard**: lock rejects a fourth. Chores and operator
  minutes are **advisory**: the UI shows overload and lets you proceed.
- Operator minutes count blocked runs and manual chores, not machine wall clock.
- Quick-add is the one content exception: the agenda screen may create a store
  task from a title and a chosen project. It still writes through the store.
- No recurrence, calendar sync, time-blocking or templates in v1.
- The ritual must take under five minutes. If it costs more than it saves, it
  has failed its own gate.

## 7. Flow

### Phase 0 — Commit

1. You change the focus set in Command Center (maximum three); Command Center
   writes the store tags.
2. The store generates and persists the day's agenda draft with a reason and
   estimate per item.
3. You edit and reorder in Command Center. Command Center validates candidate
   eligibility and locks the agenda.
4. Command Center snapshots the locked items and the agenda rev.

No locked agenda means no autonomous dispatch. A manual `cc run <task>` still
works, with a warning, and still respects gates and dependencies.

### Phase A — Shape

Shape runs through the same worktree, gate and approval loop as Build.

1. You pick a project from the store (never hardcoded).
2. A `shape-<n>` worktree branches from `cc/integration`. The inquisitor never
   writes into your checkout. The run is owned by the daemon and recorded in the
   `shapes` table; `cc run` refuses while a shape is active.
3. The inquisitor reads the repo, docs and the store summary. If docs exist, it
   audits and asks only about gaps; a rewrite is opt-in.
4. It emits `docs/spec/SPEC.md`, optional `DESIGN.md`, `docs/spec/TASKS.yaml` —
   objectives, tasks, acceptance criteria, proposed gate argv, dependencies and
   scope — and `docs/spec/QUESTIONS.yaml`, a numbered queue where every question
   carries a proposed default. Schemas and state machine: `SHAPE.md`.
5. You answer with `cc shape answer` or the web rail. Every turn — draft,
   discuss, answers, revise — is a durable `turn_jobs` row, executed one at a
   time in FIFO order by a single worker. A turn resumes the inquisitor's own
   session with the answers and the last check result, so it keeps its analysis
   instead of re-reading everything. A failed mechanical check is handed back to
   the same session twice before you see it; unanswered questions stay your work
   and never trigger a repair.
6. `cc shape materialize` creates objectives and tasks in the store from
   `TASKS.yaml`, writes the assigned ids back, and generates
   `command-center.yaml` keyed by those ids. Spec, design, tasks and gate file
   are committed in the worktree. Tasks created before approval are not eligible
   because the project phase is `shape`.
7. **Pristine** is mechanical and checked by `cc spec check`:
   - no unanswered questions and no open `TBD`/`TODO`,
   - every objective has acceptance criteria and a gate,
   - every task has a gate and `depends_on`,
   - the gate file parses and no gate is empty,
   - spec, design, tasks and gate file are committed in the worktree.
8. **Spec approval is the diff approval.** You review `cc shape diff`,
   `cc spec check` and the check output. `cc shape approve` merges the worktree
   to `cc/integration` and records `spec_state` (digest, approved_at,
   approved_in_commit, docs, phase `approved`). Tasks become build-eligible at
   that point.

Scope discovered during shaping goes to expansions first, then is promoted or
dropped. For a pre-shaped project with no Shape run, `cc spec approve <project>`
computes the digest from committed docs and records the same `spec_state`.

### Phase B — Build

1. The dispatcher selects an eligible task from the locked agenda.
2. Claim: the task is locked locally with a heartbeat and a frozen snapshot of
   its body, then conditionally set to `doing` in the store with `if_rev`.
3. Worktree: `cc/<task>-<run>` from `cc/integration`, push disabled. The base
   checkout must be clean (ignored files excluded; untracked non-ignored files
   count as dirty). `--snapshot-dirty` commits your work to a scratch ref first,
   explicitly.
4. The coder implements, writes its summary to the run directory (not the
   worktree), and does not commit.
5. **Verifier** (the program): captures the diff, then runs the gate as read from
   the integration commit, in the worktree, with a scrubbed environment. The
   worktree's gate file and `docs/spec/**` must be byte-identical to integration;
   any change aborts the run. Fail fast on the first failing command.
6. The reviewer reads the diff, task, spec digest, gate output and policy result
   in a **fresh session**. `approve` continues; `changes` consumes the attempt
   and retries with the reviewer's reasons in the coder's notes; `block` stops
   immediately.
7. One budget: coder → gate → policy → reviewer, at most `max_attempts`
   (default 3). An exhausted budget blocks the task and surfaces it.
8. **Policy scan** (deterministic) on the staged diff: secrets, `.env*`,
   binaries, files outside the task's optional `scope`, changes to
   `docs/spec/**` or `command-center.yaml`, and diff-size caps. A finding blocks
   the merge.
9. On pass: commit on `cc/<task>-<run>`, then fast-forward merge into
   `cc/integration`. Integration does not move during a run (one run per
   project); if it changed externally, abort and re-dispatch. Remove the
   worktree.
10. The task is marked `done` in the store with a run reference **after** the
    integration merge; the agenda item is `done`. An **objective** is done only
    when all its tasks are done *and* the objective gate passes.
11. **End state.** The run waits on `cc/integration`; nothing in Command Center
    merges it to master. Shipping is a plain git merge you run, and the machine
    never pushes.

A run with no diff is blocked, never auto-merged: you decide whether the task was
already done or the coder failed. Noop is not an auto-success.

Outcomes map to store task and agenda item status per `CONTRACT.md` §7. A task is
`done` only after the integration merge; an error or exhausted budget blocks it
so it cannot re-dispatch until you intervene.

## 8. Eligibility

Two full checks run in Command Center: **candidate** (at lock) and **dispatch**
(before a run). The suggester applies only the structural subset when choosing
items — project active, task open and not `doing`, gate resolves, dependencies
done or projected earlier, `spec_state` approved — and never checks base
cleanliness or model availability. Every failure returns a stable reason code
with human text and details; `cc status --json` emits the same codes. The full
list is in `CONTRACT.md` §4.

**Candidate** — at lock:

- project exists on disk, is a git repository, and is not `parked`/`archived`;
- objective status is `open` or `doing`;
- task status is `open`;
- the project has a `command-center.yaml` and the task resolves to a non-empty
  gate from `cc/integration`;
- every `depends_on` task is `done` **or appears earlier in the draft agenda**;
- `spec_state` exists, phase is `approved`/`building`, and its digest matches the
  docs at `cc/integration`;
- the base checkout is clean;
- no live lock for the task;
- model availability is a warning here, not a block.

Manual agenda items skip these checks except "task is open" and "project is
active".

**Dispatch** — candidate rechecked, plus:

- today's agenda is `locked` and its `rev` matches the snapshot;
- no other run is active for the project;
- the resolved model is up;
- every `depends_on` task is actually `done`.

**Chains:** lock validates the whole draft topologically and rejects cycles,
duplicates and dependencies placed after their dependent. If an earlier item
fails at dispatch, later items show blocked with `dep_missing`; they are not
silently skipped or reordered.

**Ordering:** agenda order first, then project priority, then task priority.

**Shape** has its own precondition: the project exists, is a git repository, is
not parked/archived, the base checkout is clean, and no run is active. It does
not require an approved spec or a gate; `cc spec check` is its gate.

Projects without a repository or without gates are visible but not runnable,
with the reason shown. v1 executes code repositories only.

## 9. Gates

`command-center.yaml` at the project root:

```yaml
version: 1
defaults:
  max_attempts: 3
  gate: []
project:
  gate:
    - ["uv", "run", "pytest", "-q"]
objectives:
  o_00000001:
    gate:
      - ["uv", "run", "pytest", "-q", "tests/test_api.py"]
tasks:
  t_00000010:
    gate:
      - ["uv", "run", "pytest", "-q", "tests/test_todo.py"]
      - ["uv", "run", "ruff", "check", "."]
    depends_on: [t_0000000f]
    scope:
      - "src/api/**"
      - "tests/test_api.py"
```

Rules:

- A gate is a list of argv commands, not a shell string. No pipes, no `&&`, no
  injection surface.
- Resolution order for a task: task gate → objective gate → project gate →
  `defaults.gate`. The first non-empty list wins.
- Commands run with `CI=1`, `NO_COLOR=1` and provider credentials stripped.
  Timeout per command; fail fast on the first non-zero exit.
- An empty gate means the task is not runnable. Declaring work without a way to
  verify it cannot silently enter the queue.
- Gate results are stored verbatim: command, exit code, duration, output tail.
- The verifier reads gate definitions from the integration commit; the worktree
  copy must match exactly. A coder cannot weaken its own gate or the tests that
  implement it. Test changes are allowed only when the task is about tests, and
  the reviewer treats weakened assertions as a block.
- `docs/spec/**` and `command-center.yaml` are immutable during Build. A change
  aborts the run and becomes an expansion.
- Style lives in the gate (`ruff format --check`, prettier…). The reviewer blocks
  on correctness, scope and spec drift; style comments are non-blocking.
- A gate change is reviewable like code, but requires a commit on
  `cc/integration` before the next dispatch. The store stays purely declarative;
  the trade-off is that it cannot display the gate or the dependency graph.

### 9.1 Policy

- A task may declare optional `scope` globs. When present, every changed file
  must match at least one glob; when absent, the scope check is skipped.
- Always blocked: `.env*`, secrets and private keys, binaries, `docs/spec/**`,
  `command-center.yaml`, and diffs over the configured file and byte caps.
- The scan runs on the staged diff after the gate. Findings are `policy_*` codes
  in `policy.json`; any finding blocks the merge.
- The permission deny list and pre-push hook are prevention; the scan is
  detection. Neither is a sandbox (`SECURITY.md` §2).

## 10. What the task store must provide

### 10.1 Read contract

- Command Center reads projects, objectives, tasks, focus tags, agendas,
  expansions and spec state. It never edits store files directly.
- The daemon polls while idle (default 30s) and on demand. No webhooks in v1.
- A task body is frozen into the run at claim. If the store's `rev` changes
  before a write-back, the push aborts with `409` and you decide; it never
  overwrites.

### 10.2 Write contract

- Only these fields are pushed: task status, `last_run`, `spec_state`, agenda
  item status and run reference, agenda lock/unlock, manual task completion,
  focus tags, and expansion drafts.
- Every write carries an idempotency key (`run id + target status`) and
  `if_rev`.
- `if_rev` is mandatory for every Command Center write. A missing revision is a
  bug, not a silent fallback.

### 10.3 Required shapes

1. **`rev` / `if_rev`** — `rev: int` on project, objective, task and the agenda
   root. Every content write increments it through one helper. UI-only writes
   (`collapsed`, board order) and agenda item `status`/`run` updates do not.
   `if_rev` is optional on PATCH/PUT bodies; Command Center always sends it.
   Stale → `409` with `{detail, current_rev}`. Legacy YAML parses as `0`. Full
   contract: `CONTRACT.md` §1.
2. **`spec_state` on a project** — metadata pushed by Command Center. The
   existing spec pointer remains the viewer path:

   ```yaml
   spec_state:
     docs: [docs/spec/SPEC.md, docs/spec/DESIGN.md]
     digest: sha256:…
     approved_at: …
     approved_in_commit: …
     dirty: false
     phase: approved        # shape | approved | building | done
   ```

   The digest covers `docs` only; `command-center.yaml` is excluded. Algorithm
   and `cc spec approve` are in `CONTRACT.md` §3.
3. **`last_run` on a task** — `{id, verdict, at}`. Full run records stay in
   Command Center, the source of truth for *how*.
4. **A warning on spec PUT** when the file is under an approved digest:
   *"editing this invalidates spec approval"*.
5. **`focus_order` on a project** — `1|2|3|null`, display only. The store renders
   it read-only. Command Center is the only writer; the store does not enforce a
   maximum.
6. **Agenda endpoints** — the store owns draft content:
   `GET/PUT /api/agendas/{date}`, `POST /api/agendas/{date}/suggest`,
   `POST /api/agendas/{date}/items`, `PATCH /api/agendas/{date}/items/{id}`,
   `DELETE /api/agendas/{date}/items/{id}`. Command Center owns the commitment:
   `POST /api/agendas/{date}/lock` and `/unlock` on its own API. Details:
   `AGENDA.md`.
7. **Agenda status** is `draft|locked`; locked agendas reject content edits with
   `409`. Item execution status and run id are the only fields Command Center
   may patch after lock.

Nothing about gates, dependencies, autonomy or models goes into the store.

## 11. Surfaces

All clients of one daemon (`ccd`, default port `4190`):

| Surface | Role | Scope |
|---|---|---|
| **CLI** | parity and scripting; the fallback when the GUI is broken | every action |
| **Web** | phone and LAN | status, agenda review and lock, run detail, logs |
| **Native cockpit** | desktop daily driver | agenda, queue, run detail, settings, tray; `cc-chat` agent, no chat UI |

`GET /api/events` (SSE): `run.started`, `run.finished`, `gate.failed`,
`policy.failed`, `run.merged`, `run.blocked`, `agenda.locked`,
`agenda.unlocked`, `model.down`, `review.verdict`. Each event carries an id; a
client reconnecting with `Last-Event-ID` receives the in-memory replay of the
last 100 events.

### 11.1 Native cockpit

PySide6, native widgets, no webview. It is a client: every action is an API call,
and it never touches SQLite or state files directly. Close hides to tray.

## 12. Runtime model

- **Daemon**: one `ccd` process owns SQLite, locks, worktrees and model slots.
  `systemd --user` unit. Clients never write the database.
- **Locks**: one local lock per task with a heartbeat. A stale claim is reaped
  after a timeout and the run is marked `error`.
- **Concurrency**: one run per project; several projects in parallel. Model
  slots: one per local provider, many for a hosted one. No two runs share a
  worktree.
- **Integration branch**: `cc/integration` per repo, created at bootstrap from
  master. Tasks branch from it; a run that passes gate + policy + reviewer
  auto-merges back. It is frozen while a run is active, so the merge is always
  fast-forward.
- **Base checkout**: must be clean at dispatch (ignored files excluded;
  untracked non-ignored files count as dirty). `--snapshot-dirty` commits your
  work to a scratch ref explicitly.
- **Worktrees**: under the state dir, `worktrees/<project>/<run>`, removed after
  the run closes; patches and records are retained.
- **Run kinds**: `shape` and `build`. Shape records live in the `shapes` table
  and use `cc spec check`; Build uses the project gate. Only one active shape or
  build per project.
- **Budgets**: `max_attempts` (default 3) and wall clock per agent. Token and
  cost accounting exists per turn; enforcement is a backlog item.
- **Shape artifacts** under `state/shapes/<shape-id>/`: `prompt.txt`,
  `agent.jsonl`, `agent.stderr.log`; the worktree lives under
  `state/worktrees/<project>/shape-<n>`.
- **Artifacts per run** under `state/runs/<run-id>/`: `prompt.txt`,
  `review-prompt.txt`, `agent.jsonl`, `agent.stderr.log`, `diff.patch`,
  `gate.json`, `policy.json`, `review.json`, `run.json`, `CHANGES.md`.
  `agent.jsonl` is written incrementally and includes the coder stream; the
  reviewer's own session is recorded separately. Run summaries live here, never
  in the worktree.
- **Run states**: `queued`, `running`, `gate_failed`, `policy_failed`,
  `review_changes`, `merging`, `merged`, `blocked`, `error`, `cancelled`.
  Shipping is not a run state because it is not a Command Center action.
- **Replay**: the session id and the exact model string are stored, so a bad run
  can be resumed by hand. A `cc replay` command is a backlog item.
- **Git safety**: never push. Do not set `remote.*.pushurl` on a worktree — it is
  repo-shared and would break your own pushes. Use a per-worktree
  `core.hooksPath` pre-push reject plus the agent permission deny.

## 13. Non-goals

- Project/objective/task/agenda CRUD; that is the task store's job.
- Voice, avatar, camera.
- General-purpose cron or CI.
- Multi-user accounts, public exposure, hosted service. A single shared token
  for LAN access is in scope.
- Autonomous pushes, PRs or publication.
- Automatic merge of `cc/integration` into master; that is your action.
- Per-task human approval of each diff.
- The store editing the focus tag or locking agendas.
- Self-grading: no agent decides that its own work is done.
- Model selection in project configuration.
- Recurrence, calendar integration, time tracking, auto-created agenda items.
- Chat that creates tasks, edits code or dispatches runs.
