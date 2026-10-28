from __future__ import annotations

from pathlib import Path
from typing import Any

from .plan import Plan
from .runs import RunRecord
from .taskstore import TaskStoreObjective, TaskStoreProject, TaskStoreTask


def build_reviewer_prompt(
    *,
    record: RunRecord,
    project: TaskStoreProject,
    objective: TaskStoreObjective,
    task: TaskStoreTask,
    plan: Plan,
    scope: list[str],
    item_brief: str | None = None,
    resumed: bool = False,
) -> str:
    gate = plan.gate_for(objective.id, task.id) or []
    gate_text = "\n".join(f"  - {' '.join(command)}" for command in gate)
    scope_text = ", ".join(scope) if scope else "(no explicit scope)"
    spec = project.spec_state
    docs = ", ".join(spec.docs) if spec else "(none)"
    digest = spec.digest if spec else "(unapproved)"
    notes = "\n".join(f"  - {note}" for note in record.notes) or "  - none"
    history = (item_brief or "").strip() or "  - none (first run for this item)"
    resume_line = (
        "This session reviewed an earlier attempt of this item. The worktree is "
        "reset to the base commit and the diff is new; re-read it rather than "
        "trusting what you saw before.\n\n"
        if resumed
        else ""
    )
    run_dir = record.run_dir
    return f"""You are the Command Center reviewer. Review exactly one task's
changes. You are read-only: do not edit files and do not run shell commands.

Run: {record.id}
Attempt: {record.attempt}
Project: {project.id} — {project.name}
Objective: {objective.id} — {objective.title}
Task: {task.id} — {task.title}
Task note: {task.note or "(none)"}
Spec docs: {docs}
Spec digest: {digest}
Allowed scope: {scope_text}

Gate commands:
{gate_text}

Previous attempt notes:
{notes}

Prior work on this item:
{history}

{resume_line}Artifacts to read:
  diff:   {run_dir}/diff.patch
  gate:   {run_dir}/gate.json
  policy: {run_dir}/policy.json
  coder summary: {run_dir}/CHANGES.md (if present)

The worktree is the current directory; the diff is already staged and is the
complete change. Judge correctness, scope, security, and spec drift against the
task. Style comments are warn/note only.

Return exactly one JSON object and nothing else:
{{
  "verdict": "approve" | "changes" | "block",
  "reasons": ["at least one short reason"],
  "findings": [
    {{"file": "src/x.py", "line": 12, "severity": "warn", "note": "..."}}
  ]
}}
"""


def build_coder_prompt(
    *,
    record: RunRecord,
    project: TaskStoreProject,
    objective: TaskStoreObjective,
    task: TaskStoreTask,
    plan: Plan,
    scope: list[str],
    previous_notes: list[str],
    item_brief: str | None = None,
    resumed: bool = False,
) -> str:
    gate = plan.gate_for(objective.id, task.id) or []
    gate_text = "\n".join(f"  - {' '.join(command)}" for command in gate)
    scope_text = ", ".join(scope) if scope else "(no explicit scope; stay in the task area)"
    notes = "\n".join(f"  - {note}" for note in previous_notes) or "  - none"
    history = (item_brief or "").strip() or "  - none (first run for this item)"
    resume_line = (
        "This session worked on an earlier attempt of this item. The worktree has "
        "been reset to the base commit, so re-read every file before you change "
        "it; do not assume your earlier edits are still there.\n\n"
        if resumed
        else ""
    )
    spec = project.spec_state
    docs = ", ".join(spec.docs) if spec else "(none)"
    digest = spec.digest if spec else "(unapproved)"
    return f"""You are the Command Center coder. Implement exactly one task in the
current worktree.

Run: {record.id}
Project: {project.id} — {project.name}
Objective: {objective.id} — {objective.title}
Task: {task.id} — {task.title}
Task note: {task.note or "(none)"}
Spec docs: {docs}
Spec digest: {digest}
Allowed scope: {scope_text}

Gate commands (run by the verifier after you finish):
{gate_text}

Previous attempt notes:
{notes}

Prior work on this item:
{history}

{resume_line}Rules:
- Work only inside the current worktree and the run directory
  {record.run_dir}.
- Do not run git commit, git push, git remote, or git config. The harness commits.
- Do not edit docs/spec/** or command-center.yaml.
- Keep changes inside the allowed scope.
- Run the gate commands yourself when useful.
- Write a concise summary of the change to {record.run_dir}/CHANGES.md.
- Do not add secrets, .env files, binaries, or generated artifacts.
- If the task cannot be completed safely, explain why in the summary and stop.

The verifier, not you, decides whether the task is done.
"""


def build_inquisitor_prompt(
    *,
    project,
    root,
    questions,
    attempt: int,
    rewrite: bool,
    seed_objective=None,
    seed_task=None,
    previous_issues: list[str] | None = None,
    instruction: str | None = None,
) -> str:
    from . import spec

    docs = spec.default_docs(root)
    docs_text = "\n".join(f"  - {doc}" for doc in docs) or "  - (none)"
    existing_rows: list[str] = []
    for objective in project.objectives:
        existing_rows.append(f"  - {objective.id} — {objective.title}")
        for task in objective.tasks:
            existing_rows.append(f"    - {task.id} — {task.title} ({task.status})")
    existing_text = "\n".join(existing_rows) or "  - (none)"
    selected_text = "  - (none)"
    if seed_objective is not None:
        selected_text = f"  - objective {seed_objective.id} — {seed_objective.title}"
        if seed_task is not None:
            selected_text += (
                f"\n  - task {seed_task.id} — {seed_task.title}\n"
                f"    {seed_task.note or '(no note)'}"
            )
    question_text = "  - (no prior questions)"
    if questions is not None:
        rows = []
        for question in questions.questions:
            answer = (question.answer or "").strip() or "(unanswered)"
            rows.append(
                f"  - {question.id}: {question.question}\n"
                f"    default: {question.default}\n"
                f"    answer: {answer}"
            )
        question_text = "\n".join(rows) or "  - (no prior questions)"
    issue_text = "\n".join(f"  - {issue}" for issue in previous_issues or [])
    if not issue_text:
        issue_text = "  - (none)"
    instruction_text = (instruction or "").strip()
    instruction_block = (
        f"\nOperator instruction for this pass:\n{instruction_text}\n\n"
        "Apply this instruction. It outranks your own priorities but not the "
        "operator's earlier answers; where the two conflict, ask instead of "
        "guessing.\n"
        if instruction_text
        else ""
    )
    resume_text = (
        "This session produced the previous pass. You already have the "
        "repository in context: do not read it again. The only file that can "
        "have changed outside this session is docs/spec/TASKS.yaml (the "
        "operator edits the tree), so re-read that one if you are about to "
        "change it."
        if attempt > 1
        else "This is a fresh one-shot session."
    )
    return f"""You are the Command Center inquisitor. Shape one project to a
pristine, implementable specification and task decomposition.

Project: {project.id} — {project.name}
Status: {project.status}
TaskStore summary: {project.summary or "(none)"}
TaskStore next: {project.next or "(none)"}
Checkout: {root}
Existing spec docs:
{docs_text}
Selected work item (shape priority):
{selected_text}
Existing TaskStore decomposition:
{existing_text}
Rewrite allowed: {rewrite}
Shape attempt: {attempt}

{resume_text} You cannot ask interactive questions and you cannot run shell
commands or use the network. Read the repository, its AGENTS.md, existing
docs, the TaskStore summary, and the current shape files.

Work in one pass. Once you know what to write, write all four files in a single
batch of tool calls and stop. Do not write one file, re-read it, write it again,
and audit yourself with more reads: every extra read is another round-trip over
this session's whole context. Read what you need first, then write.

Last check result:
{issue_text}

Adoption rule: if docs exist, audit them and ask only about gaps. Do not rewrite
a working spec unless "Rewrite allowed" is true. If the repo has a root SPEC.md
or DESIGN.md, treat it as legacy input and produce the canonical docs under
docs/spec/.

Preservation rule: where the existing TaskStore decomposition still fits, keep the
same objective and task titles so the harness can adopt the existing IDs. Do not
write taskstore_id values; the harness attaches them by title after your draft.
Prioritise the selected work item above the rest of the project.

Prior question queue:
{question_text}

{instruction_block}
On a revision, incorporate answered questions. Add any genuinely new questions
with proposed defaults. Leave unanswered questions in place; do not answer
them yourself.

Write exactly these files, relative to the checkout:

1. docs/spec/SPEC.md — required canonical spec.
2. docs/spec/DESIGN.md — optional; include only when design decisions matter.
3. docs/spec/TASKS.yaml — required decomposition.
4. docs/spec/QUESTIONS.yaml — required question queue (may be empty).
5. docs/spec/USE_CASES.yaml — the scenarios a user or another system performs.
   Write it whenever the project has flows someone actually goes through (a UI,
   a CLI, a service other things call). Skip it for pure infrastructure.

Do not write command-center.yaml. The harness generates it from TASKS.yaml after
TaskStore IDs exist. Do not write taskstore_id values; leave them absent.

TASKS.yaml schema (strict; unknown fields are rejected):

version: 1
objectives:
  - slug: auth
    title: Authentication
    detail: One paragraph. Optional.
    acceptance:
      - A concrete, testable objective outcome.
    gate:
      - ["uv", "run", "pytest", "-q"]
    tasks:
      - slug: session-schema
        title: Add the session schema
        detail: One paragraph. Optional.
        acceptance:
          - A concrete, testable task outcome.
        gate:
          - ["uv", "run", "pytest", "tests/test_session.py", "-q"]
        depends_on: []
        scope:
          - "src/auth/**"
          - "tests/test_session.py"

Rules for TASKS.yaml:
- Every objective and task needs at least one acceptance criterion.
- Every objective and task needs a non-empty gate argv.
- Every task needs a depends_on list (may be empty) and a scope list.
- Dependency slugs are global across objectives; every dependency must exist;
  cycles are rejected.
- Keys and slugs are lower-case kebab-case.
- Gates must be runnable by the project harness, not aspirational prose.

QUESTIONS.yaml schema (strict):

version: 1
questions:
  - id: q1
    question: A single decision the operator must make.
    default: The inquisitor's proposed default.
    answer: null
    note: Optional context.

Rules for QUESTIONS.yaml:
- Every question has a proposed default.
- The operator answers; every answer must be non-null before approval.
- No open TBD or TODO markers anywhere in SPEC/DESIGN/TASKS/QUESTIONS.
- If there are no open questions, use `questions: []`.
- When you revise, preserve existing answers.

USE_CASES.yaml schema (strict; unknown fields are rejected):

version: 1
use_cases:
  - id: uc1
    title: Ask a question and hear the answer
    actor: Operator
    summary: One line of intent. Optional.
    steps:
      - id: s1
        task: wake-word
        label: Say the wake word
      - id: s2
        task: transcribe
        label: Transcribe the request
      - id: s3
        label: Does the phrase match a known request?
        kind: choice
        branches:
          - guard: yes
            to: s5
          - guard: no
            to: s4
      - id: s4
        label: Ask the operator to repeat it
      - id: s5
        label: Route to the local model

Rules for USE_CASES.yaml:
- One use case per scenario a person or another system actually performs.
- The main flow is in order. Put an alternate flow in its own use case.
- A step may name a task slug from TASKS.yaml in `task:`; the slug must exist.
  Leave `task` off for a step that has no implementing task yet.
- A real decision is a `kind: choice` step with a diamond, not an activity that
  says "if". Give every branch a short guard (yes/no, or the condition) and a
  `to:` step id that comes LATER in the same flow. Branches never point
  backwards and never leave the use case. A choice with no branches is allowed
  while the branch is still undecided.
- Step ids are unique inside a use case (`s1`, `s-auth`); give every step one
  when the flow has a choice.
- The operator edits this diagram by hand; keep the steps short and readable.
- Do not invent work here. TASKS.yaml is the decomposition; this file is how the
  pieces are used.

Also:
- No `TBD`, `TODO`, or unanswered decision may remain in the committed docs.
- Do not include a gate file, secrets, or changes outside docs/spec/.
- Do not commit. The harness commits.
- End with a short summary of what changed and what remains as questions.

The harness will run `cc spec check`, materialize TaskStore objectives/tasks, and
ask the operator to approve the shape diff. Make the docs and decomposition
worth approving.
"""


def build_use_cases_prompt(
    *, project: Any, root: Path, tasks: Any, existing: str | None = None
) -> str:
    """A turn that only writes USE_CASES.yaml, derived from the spec.

    With `existing`, the pass updates the file the operator is already shaping
    instead of deriving it from nothing.
    """
    slugs = [
        task.slug
        for objective in (tasks.objectives if tasks is not None else [])
        for task in objective.tasks
    ]
    slug_text = ", ".join(slugs) if slugs else "(no tasks yet)"
    if existing is None:
        mode = """This is a fresh derivation: there is no USE_CASES.yaml yet."""
        closing = "End with a one-line summary of the flows you found."
    else:
        mode = f"""The operator already shapes this file. This is an UPDATE, not a
rewrite: keep what is there — ids, titles, actors, step order, labels, task
links, and every `kind: choice` with its branches — and change only what the
spec contradicts or what is missing. Add use cases the spec describes and the
file does not, fix links to tasks that no longer exist, and refine a step only
when the spec is more specific. Do not renumber ids, do not re-title flows the
operator wrote, and do not drop a flow just because it is short.

Current file:

{existing.strip()}
"""
        closing = (
            "End with a one-line summary of what you changed and what you left "
            "alone."
        )

    return f"""You are the Command Center inquisitor. Derive the workflows for one
shaped project. This pass writes use cases and nothing else.

Project: {project.id} — {project.name}
Status: {project.status}
Checkout: {root}

Read docs/spec/SPEC.md, docs/spec/DESIGN.md if it exists, and
docs/spec/TASKS.yaml. The spec says what must be true; the tasks say what gets
built; your job is the third view: what a person or another system actually
does, in order.

{mode}

Write docs/spec/USE_CASES.yaml:

version: 1
use_cases:
  - id: uc1
    title: Ask a question and hear the answer
    actor: Operator
    summary: One line of intent.
    steps:
      - id: s1
        task: {slugs[0] if slugs else "task-slug"}
        label: Say the wake word
      - id: s2
        label: Does the phrase match?
        kind: choice
        branches:
          - guard: yes
            to: s3
          - guard: no
            to: s4
      - id: s3
        label: Route to the local model
      - id: s4
        label: Ask the operator to repeat it

These are the only task slugs that exist. Link a step with `task:` only when
one of them implements it; leave `task` off otherwise:
{slug_text}

Rules:
- One use case per scenario a person or another system actually performs.
- The main flow is in order. Put an alternate flow in its own use case.
- Steps are short verb phrases, not task titles copied verbatim.
- A step where the flow decides something is `kind: choice` with two branches
  (guard + `to:` a LATER step id). Do not write "if ..." into an activity.
  Branches point forward only and never leave the use case.
- Cover the whole spec: every user-facing capability should appear in at least
  one flow. Do not invent capabilities the spec does not describe.
- Ids are unique (uc1, uc2, uc-auth).
- Step ids are unique inside a use case; a choice branch must point at a LATER
  step id.
- Write no other file. Do not commit. The harness commits.

{closing}
"""


def build_c4_prompt(
    *, project: Any, root: Path, tasks: Any, existing: str | None = None
) -> str:
    """A turn that only writes C4.yaml: context, container, component, code."""
    if existing is None:
        mode = "This is a fresh derivation: there is no C4.yaml yet."
        closing = "End with a one-line summary of the architecture you found."
    else:
        mode = f"""The operator already shapes this file. This is an UPDATE, not a
rewrite: keep every element id, name and description, and every relationship,
unless the spec contradicts it. Add what is missing, fix relationships whose
ends no longer exist, and only change a level the spec now describes
differently.

Current file:

{existing.strip()}
"""
        closing = (
            "End with a one-line summary of what you changed and what you left "
            "alone."
        )

    return f"""You are the Command Center inquisitor. Model the architecture of one
shaped project in C4. This pass writes architecture and nothing else.

Project: {project.id} — {project.name}
Status: {project.status}
Checkout: {root}

Read docs/spec/SPEC.md, docs/spec/DESIGN.md if it exists, and
docs/spec/TASKS.yaml. The spec says what the system must do; this file says what
it is made of.

{mode}

Write docs/spec/C4.yaml with all four levels:

version: 1
levels:
  context:
    elements:
      - id: user
        type: person
        name: User
        description: Uses the system from a browser.
      - id: service
        type: system
        name: Example service
        description: The system in scope.
    relationships:
      - from: user
        to: service
        label: sends a request
        technology: HTTPS
  container:
    elements: []
    relationships: []
  component:
    elements: []
    relationships: []
  code:
    elements: []
    relationships: []

Element types: person, system, external, container, component, database, queue,
code, decision.

What goes on each level:
- context: the people and software systems that use or are used by this system.
  No containers, no components. This is the only level with `person`.
- container: the separately deployable/runnable units and data stores inside the
  system (an app, a service, a database, a queue).
- component: the significant parts inside those containers and how they talk.
- code: classes or interfaces only where the design decision matters. An empty
  level is a valid answer.

`decision` is borrowed from UML activity notation, not C4: use it only where a
flow between elements genuinely branches, and put the guard in the relationship
label ("[yes]", "[no match]"). Never use it to restate an element's behaviour.

Rules:
- Ids are kebab-case and unique inside a level.
- Every relationship names two elements that exist in that same level.
- A relationship label is a verb phrase ("reads", "publishes to"), not a noun.
- Technology is one word or a short phrase; omit it when it adds nothing.
- Do not invent components the spec does not imply.
- Write no other file. Do not commit. The harness commits.

{closing}
"""


def build_repair_prompt(*, issues: list[str], attempt: int) -> str:
    rows = "\n".join(f"  - {issue}" for issue in issues) or "  - (none)"
    return f"""The shape check rejected your previous pass. These are the exact issues:

{rows}

Fix every one of these problems, in place, in the current checkout. Repair
attempt {attempt}. Do them all in one batch of edits — do not fix the first
issue, stop, and wait to be told about the rest.

Rules:
- Do not re-open decisions, do not rewrite the spec, do not add questions.
- Keep every existing answer in QUESTIONS.yaml exactly as it is.
- TASKS.yaml and QUESTIONS.yaml must stay valid YAML.
- A scalar that starts with a quote character is parsed as a quoted scalar, so
  `- "word" and more words` is invalid: everything after the closing quote
  breaks the parse. Either wrap the whole value in one pair of quotes, or do
  not start the value with a quote character. This is the failure that keeps
  coming back.
- Quote any value that contains `: `, ` #`, a leading `-`, `?`, or `*`.
- Keep the existing objective and task titles so TaskStore ids are adopted.
- Do not write taskstore_id values, command-center.yaml, or anything outside
  docs/spec/. Do not commit. The harness commits.
- Run no shell commands and use no network.

When the files are fixed, stop. The harness re-runs the check.
"""


def build_discuss_prompt(
    *,
    project,
    record,
    questions,
    message: str,
    question_id: str | None = None,
) -> str:
    rows = []
    for question in (questions.questions if questions else []):
        answer = (question.answer or "").strip() or "(unanswered)"
        marker = "  <- the operator is asking about this one" if question.id == question_id else ""
        rows.append(
            f"  - {question.id}: {question.question}{marker}\n"
            f"    default: {question.default}\n"
            f"    answer: {answer}"
        )
    queue_text = "\n".join(rows) or "  - (no questions)"
    focus = next(
        (
            question
            for question in (questions.questions if questions else [])
            if question.id == question_id
        ),
        None,
    )
    focus_text = (
        f"Question in focus: {focus.id} — {focus.question}\n"
        f"Default: {focus.default}\n"
        f"Answer so far: {(focus.answer or '').strip() or '(unanswered)'}"
        if focus is not None
        else "No single question is in focus."
    )
    return f"""The operator is thinking out loud with you about the draft for
{project.id} — {project.name}. This is a discussion turn: reply only, do not
edit anything.

{focus_text}

Question queue:
{queue_text}

Operator's message:
{message}

Answer the message using what you know about this repository and the draft.
If the decision is theirs, give the smallest set of options that settles it and
say what you recommend. If you need a fact from the repo, read it. If the
operator points at a link and the answer turns on what is behind it, fetch it —
say what it actually says and say when it fails. End with one
line: "SUGGESTED ANSWER: ..." only when you are recommending how to answer the
focused question; otherwise omit it.
"""
