# Command Center

A local-first execution layer for AI agents. Work is declared in a **task
store**; Command Center commits a small daily agenda, shapes a project's
specification until it is checkable, decomposes it into tasks, and drives each
task to done — with a writer, a gate, and an independent reviewer that did not
write the code.

Nothing leaves the machine. Nothing is pushed.

## Boundary

Command Center is not a task manager and not a chat UI. It executes.

| System | Role | Owns |
|---|---|---|
| **Task store** (external) | what should be done | projects, objectives, tasks, focus tags, agendas, spec pointer |
| **Command Center** | doing it | queue consumption, locks, worktrees, runs, gates, approvals, focus list, agent files |
| **Clients** | operating it | CLI, web, native cockpit |

The task store is any service implementing the contracts in
[`CONTRACT.md`](CONTRACT.md) — revisions (`rev`/`if_rev`), agendas, focus tags,
`spec_state` and `last_run`. Command Center keeps a task snapshot and its own
run history; it never duplicates the plan.

## The loop

**Day — commit.** Pick up to three focus projects, review the suggested agenda,
lock what will actually run. Only locked items dispatch.

**Project — shape.** Argue the spec to a close through a question queue, write
`docs/spec/TASKS.yaml`, run the pristine check, materialize objectives and tasks
into the task store, approve the spec digest. See [`SHAPE.md`](SHAPE.md).

**Task — build.** coder → gate → policy scan → independent reviewer →
`cc/integration`. One worktree per work item, one attempt budget per run. Every
attempt records its prompt, stream, diff, gate result, review and outcome.

**Land.** Verified work merges into `cc/integration` automatically. Command
Center stops there — moving integration to master is a plain git merge you run
yourself.

## Non-negotiables

- **Verification is code.** The gate and the policy scan are scripts, not
  agents. The writer never grades itself.
- **The reviewer is a separate session** and never sees the writer's messages.
- **The target repository cannot configure the executor** — no models, no agent
  permissions, no mid-run gate changes. The code being judged must not control
  the judge.
- **A run is recorded or it did not happen.** Prompt, logs, diff, gate, review,
  model, timing, cost.
- **Nothing is pushed.** A per-worktree `pre-push` hook and the agent shell deny
  list refuse it.

## Agents

Three pipeline agents — inquisitor, coder, reviewer — plus one interface agent
for read-only discussion. Each is a fixed agent file with its own model,
permissions and output contract; see `agents/`. Verification and policy are
ordinary code.

## Surfaces

One daemon owns all state and exposes one versioned HTTP API.

- **CLI** — `cc status|tasks|models|focus|agenda|spec|run|runs|shape`.
- **Web** — `http://<host>:4190/` with a phone-first cockpit: Overview,
  Objectives, Inquisitor, Builder.
- **Cockpit** — native PySide6 window, tray notifications, incremental logs.

No client holds state or business logic.

## Quickstart

```sh
uv sync
git config core.hooksPath .githooks   # refuse commits that carry config or secrets
cp config.example.yaml config.yaml    # then edit: PIN, task-store URL, model keys

# provider keys stay outside the repo; the systemd unit loads this file
printf 'PROVIDER_API_KEY=your-key\n' > ~/.config/command-center/env
chmod 600 ~/.config/command-center/env

uv run ccd                            # or install systemd/command-center.service
uv run cc agents install              # inquisitor + coder + reviewer + chat agents
uv run cc status
uv run cc run <task>

# shape a project
uv run cc shape start <project> [--objective <o_id> --task <t_id>]
uv run cc shape status <project>
uv run cc shape answer <project> q1 "your answer"   # queues the turn, waits for it
uv run cc shape revise <project> [--instruction "..."]
uv run cc shape jobs <project>                      # the turn queue
uv run cc shape check <project>
uv run cc shape materialize <project>
uv run cc shape diff <project>
uv run cc shape approve <project>

# web
# open http://localhost:4190/ and enter the PIN from your config

# cockpit
uv sync --extra cockpit
uv run cc-cockpit
```

Optional desktop entry and icon:

```sh
mkdir -p ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
cp desktop/command-center-cockpit.desktop ~/.local/share/applications/
cp desktop/command-center-honeycomb.svg \
   ~/.local/share/icons/hicolor/scalable/apps/command-center.svg
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

Run `uv run pytest` and `uv run ruff check src tests` before sending a change.

## Documentation

| File | Contents |
|---|---|
| [`SPEC.md`](SPEC.md) | the design: decisions, boundary, flow, eligibility, gates, runtime |
| [`CONTRACT.md`](CONTRACT.md) | wire contracts: revisions, eligibility codes, HTTP API, SSE, run/gate/review records |
| [`AGENDA.md`](AGENDA.md) | the daily agenda: ownership, schema, transitions, endpoints, locking |
| [`SHAPE.md`](SHAPE.md) | the shape loop: files, state machine, questions, pristine check, materialization |
| [`SECURITY.md`](SECURITY.md) | threat model, agent runtime, secrets, git, network, filesystem |

## License

MIT — see [`LICENSE`](LICENSE).
