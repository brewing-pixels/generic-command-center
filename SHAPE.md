# Shape — Contract

*Status: P5 implemented. Companion to `SPEC.md` §7 Phase A and `CONTRACT.md`.*

## 1. Purpose

Shape turns a project from messy docs into:

- a canonical spec and optional design,
- a numbered question queue,
- a task decomposition with gates, dependencies, and scope,
- store objectives/tasks and a generated `command-center.yaml`,
- an approved spec state whose tasks are build-eligible.

The inquisitor is one-shot and read-only outside `docs/spec/`. The harness owns
git, the store, and the gate file. The operator approves the shape diff; nothing is
merged to `cc/integration` until `cc shape approve`.

## 2. Files

All paths are relative to the project checkout.

| Path | Owner | Required |
|---|---|---|
| `docs/spec/SPEC.md` | inquisitor | yes |
| `docs/spec/DESIGN.md` | inquisitor | optional |
| `docs/spec/TASKS.yaml` | inquisitor; harness adds `taskstore_id` | yes |
| `docs/spec/QUESTIONS.yaml` | inquisitor; operator answers | yes |
| `command-center.yaml` | harness | generated at materialize |

Legacy root `SPEC.md`/`DESIGN.md` are read as input. The canonical output is
`docs/spec/`. Root docs are not moved or deleted by P5; remove or archive them
in a normal commit.

## 3. State machine

`prepare` creates a `shape-<n>` worktree from `cc/integration` and sets the
store spec phase to `shape`.

A shape can be **seeded** with an existing store objective and task. The
inquisitor gets that work item as the priority and the existing decomposition as
preservation context. This is still one project-level shape; the seed is the
entry point, not a separate spec. At materialization the harness adopts matching
existing objective/task ids by normalized title, so the selected agenda item
keeps its task id when the decomposition preserves it.

| State | Meaning |
|---|---|
| `drafting` | initial inquisitor run in progress |
| `questions` | inquisitor output has unanswered questions |
| `answers_ready` | all current questions answered; ready for a revision |
| `revising` | inquisitor revision in progress (resumes the draft session) |
| `ready` | pristine check passes; no gate file materialized yet |
| `check_failed` | pristine check has issues |
| `materialized` | store IDs exist and the final gate file passes |
| `approved` | merged to integration; spec state approved |
| `cancelled` | worktree removed; previous spec state restored |
| `error` | last operation failed; inspect/cancel/retry |

Only one active shape or build run per project. `cc run` refuses while a shape
is active. Starting a build while a shape is active is also rejected.

## 4. Commands

The daemon owns the state; the CLI is the P5 UI.

```sh
cc shape start <project> [--rewrite] [--objective <o_id>] [--task <t_id>]
                                       # prepare worktree + run inquisitor
cc shape status <project> [--json]     # state, questions, last check
cc shape answer <project> <qid> <text>
cc shape revise <project>              # resume the inquisitor with answers
cc shape check <project> [--json]      # pristine check on the active worktree
cc shape materialize <project>         # create store tasks + gate file
cc shape diff <project> [--json]       # review the shape batch diff
cc shape approve <project>             # check, merge to integration, approve
cc shape cancel <project>              # remove worktree, restore prior state
cc spec check <project> [--json]       # same check, picked by active shape
```

`start` and `revise` are asynchronous; poll `status`. `answer`, `check`,
`materialize`, `approve`, `cancel`, and `diff` are synchronous.

The same operations are exposed at:

```text
POST /api/shapes                      # {project, rewrite?, objective?, task?}
GET  /api/shapes/{project}
POST /api/shapes/{project}/answer
POST /api/shapes/{project}/revise
POST /api/shapes/{project}/check
POST /api/shapes/{project}/materialize
POST /api/shapes/{project}/approve
POST /api/shapes/{project}/cancel
GET  /api/shapes/{project}/diff
```

## 5. Questions

`docs/spec/QUESTIONS.yaml`:

```yaml
version: 1
questions:
  - id: q1
    question: Which persistence layer should the first slice use?
    default: SQLite via the existing store
    answer: null
    note: Optional context.
```

- IDs match `q[0-9A-Za-z_-]+`.
- Every question has a proposed default.
- `answer: null` or blank blocks `cc spec check`.
- `cc shape answer` writes and commits the answer.
- A revision is a fresh inquisitor session. It must preserve answers and add only
  genuinely new questions.
- Revisions are bounded by `budgets.max_attempts` (default 3: initial draft plus
  two revisions). There is no separate critic artifact; the fresh revision
  session is the critique.

## 6. TASKS.yaml

```yaml
version: 1
objectives:
  - slug: auth
    title: Authentication
    detail: Optional paragraph.
    acceptance:
      - A testable objective outcome.
    gate:
      - ["uv", "run", "pytest", "-q"]
    taskstore_id: o_...
    tasks:
      - slug: session-schema
        title: Add the session schema
        detail: Optional paragraph.
        acceptance:
          - A testable task outcome.
        gate:
          - ["uv", "run", "pytest", "tests/test_session.py", "-q"]
        depends_on: []
        scope:
          - "src/auth/**"
        taskstore_id: t_...
```

Rules:

- Objective/task slugs are global kebab-case identifiers.
- Every objective and task has non-empty acceptance criteria and a non-empty
  gate argv.
- Every task has `depends_on` (may be empty) and `scope`.
- Dependencies must exist and be acyclic.
- `taskstore_id` is absent in inquisitor output; the materializer fills it.
- Unknown fields are rejected.

## 7. Pristine check

`cc spec check` is mechanical:

- `SPEC.md` exists; `DESIGN.md` optional.
- No open `TBD` or `TODO` outside fenced code in SPEC/DESIGN/TASKS/QUESTIONS.
- `QUESTIONS.yaml` has no unanswered question.
- TASKS schema, gates, acceptance, dependencies, and cycles are valid.
- The worktree is clean and the docs/TASKS/QUESTIONS are committed.
- Before materialization, TASKS proposed gates are enough.
- After materialization, every objective/task has a `taskstore_id`, the gate file
  parses, every objective/task has an explicit gate, and dependencies/scope match
  the mapped store ids.
- After materialization, `command-center.yaml` is committed.

A failed check records the issues in the shape record and does not approve.

## 8. Materialization

`cc shape materialize`:

1. Loads TASKS and verifies the pre-materialization check.
2. Adopts existing store ids by normalized objective/task title where possible.
3. Creates missing store objectives under the project.
4. Creates missing store tasks under their objective.
5. Writes assigned `taskstore_id`s back into TASKS after each creation (retry-safe).
6. Generates `command-center.yaml` keyed by store objective/task ids.
7. Commits TASKS + gate file.
8. Runs the materialized check.

store tasks exist while `spec_state.phase = shape`, so they are visible but not
build-eligible. A cancelled/rejected shape leaves them open and droppable; P5
does not delete them.

## 9. Approval and cancellation

`cc shape approve`:

1. Requires a passing materialized check.
2. Requires the shape worktree clean.
3. Requires `cc/integration` to still be the shape base (or already the shape
   head on retry).
4. Moves `cc/integration` to the shape head. There is no push.
5. Computes the SPEC+DESIGN digest from the integration commit.
6. Patches store `spec_state`: docs, digest, `approved_at`,
   `approved_in_commit`, `dirty: false`, `phase: approved`.
7. Removes the shape worktree. The shape branch remains for audit.

`cc shape cancel` removes the worktree and restores the previous
`spec_state`. If there was no previous spec state, it leaves `phase: shape`.

Shape approval does not push or touch master; `cc/integration` stays where it
is until the operator merges it by hand.

## 10. Adoption

- Existing docs are audited, not rewritten, unless `--rewrite` is passed.
- Canonical new docs go under `docs/spec/`.
- Root docs are input; they remain until the operator removes them in a reviewed
  diff.
- At materialization, objective and task titles are compared to the existing
  store decomposition (normalized, exact title). Matches reuse the existing
  ids; unmatched proposals are created. Existing tasks that are not proposed
  are left untouched, not deleted.
- `command-center.yaml` is regenerated for the adopted/created ids.
- The selected seed ids are preserved only when the inquisitor keeps the matching
  titles. The approval diff is the check; if the selected agenda task is not
  adopted, its work sheet reports the remaining blockers.
- A web work item page exposes the same operations from Today; the cockpit
  still has no Shape screen. The page header chooses the build model from the
  configured allowlist; the reviewer is a separate session on the
  configured reviewer model. A shape is reviewed with
  `cc shape diff` and
  `cc spec check` before `cc shape approve`.

## 11. Scope

P5 was CLI-first; the web work item page is the first browser shape surface. It
uses the same daemon state machine and API. The P4 cockpit continues to show
execution, not shape.
