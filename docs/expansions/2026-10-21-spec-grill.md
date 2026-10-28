# Expansion — Spec grill (requirements interview)

*Status: raw. Captured 2026-10-21. Not scheduled, not shaped, no code.*
*Related: `SPEC.md` §5 (Phase A — Shape).*
*Store: expansion `spec-grill`, project `command-center`.*

## Why this exists

`SPEC.md` v2 says Phase A produces a spec, a design, and a question queue — but
it does not say *how* the questions get asked. The "grind until the
specification is pristine" mechanic was left as an assumption.

It turns out the mechanic is already codified in public repos under names like
**grill-me**, **requirements interview**, and **adversarial requirements
interview**. This expansion records what exists, what to take, what to refuse,
and the next step. It does not change the locked v2 design.

## Prior art (checked 2026-10-21)

| Repo | License | What it is | Take | Refuse |
|---|---|---|---|---|
| **rjmurillo/ai-agents** | MIT | 30-agent toolkit for Claude Code / Copilot CLI; lifecycle `/spec → /plan → /build → /test → /review → /ship`; critic verdicts; ADR-steered behaviour | Critic verdict vocabulary: `APPROVED`, `APPROVED_WITH_CONCERNS`, `NEEDS_REVISION`, `BLOCKED`. ADRs as steering artifacts. | 30 agents, orchestrator routing, plugin marketplace. This is the work system again, larger. |
| **karthikrshet/aiskills** | Apache-2.0 | 22 skills in `SKILL.md` format; `grill-me` + `grill-with-docs`; templates for SPEC / DESIGN / ADR / CONTEXT / REVIEW; HITL tiers Inform / Consult / Approve; `git-guardrails` | The four artifacts (SPEC, DESIGN, ADR, CONTEXT glossary). "Never start writing code until grilling is complete." Guardrails confirm our no-push stance. | CLI, MCP server, 22-skill catalog, 6 workflows, 13 mandatory sections per skill. |
| nicobailon/grill-for-unknowns | reported, not checked | Unknowns taxonomy + grill session ledger; one decision at a time; confirm before enacting | The unknowns taxonomy and the ledger idea. | One decision at a time — wrong for a limited time budget. |
| unboundinnov/specdd | reported, not checked | Interview for gaps → spec + plan → build only after "ship it" | Explicit "ship it" trigger before implementation. | — |
| dnhkng/specfill | reported, not checked | TUI that interviews one question at a time | — | TUI surface. |
| harshilparmar/spec-assistant | reported, not checked | Six-area framework with drill-down rounds | Category coverage as a checklist. | — |
| team-attention/hoyeon | reported, not checked | Interviewer agent, "questions-only, never builds" | Confirms: interview runs in the read-only `plan` agent. | — |

Only the first two were verified (repo READMEs, licenses). The rest are leads,
not evidence.

## What none of them do

Every one of them stops at spec and plan. None dispatches work, isolates it in a
worktree, runs a gate the writer did not write, records the run, or syncs status
back to a plan store. That remains the whole of Command Center. The grill is an
input layer, and it is the cheapest part to acquire.

## Proposed shape (still unshaped)

- **The grill is a skill, not code.** A `SKILL.md` loaded by the `plan` agent in
  the Shape call-site. Editable without touching Command Center, and usable
  interactively outside it ("grill me on X").
- **Rounds, not a waterfall.** Each round is a batch of questions, each with a
  recommended default, tagged `requirements / domain / technical / scope /
  risk`. The operator answers `A` (accept), a correction, or `defer`.
- **Four artifacts**, all in the project repo:
  - `docs/spec/SPEC.md` — requirements and acceptance criteria
  - `docs/spec/DESIGN.md` — technical design
  - `docs/spec/CONTEXT.md` — ubiquitous language / glossary
  - `docs/spec/DECISIONS.md` — chosen option, rejected alternative, why
- **A ledger** — `docs/spec/OPEN-QUESTIONS.md` during shaping. Must be empty at
  approval.
- **Termination is mechanical**, not a feeling: a dry round produces no new
  questions, the ledger has zero open rows, every objective has a gate, every
  task has a gate and dependencies.
- **Round cap: 3.** Unanswered low-risk questions become explicit assumptions in
  `DECISIONS.md`, not blockers. "Pristine" must not become perfectionism.
- **Consistency audit.** A fresh `plan` session compares SPEC against
  `DECISIONS.md` for contradictions and silent omissions before approval.
- **Digest** covers SPEC + DESIGN + CONTEXT + DECISIONS. If a task later
  contradicts a recorded decision, that is spec drift, not a coder mistake.
- **The ledger must include market questions**, not only technical ones: who
  pays, what they do today, why they would switch. Projects here do not start
  from tickets; they start from an idea, so the interview is the only place a
  customer can enter the loop. A grill that produces only architecture is a
  sophisticated way to avoid the market.

## Risks

- **Grill as productivity theatre.** It feels like progress and produces
  documents. The round cap and the ship gate exist to contain it.
- **Perfectionism loop.** Without a cap, "pristine" never arrives.
- **Vocabulary drift.** If `CONTEXT.md` is not part of the digest, the coder
  and the spec slowly stop speaking the same language.
- **Stock `plan` prompt is generic.** All role behaviour lives in the skill
  text; that is acceptable for output shape but not for tool policy.
- **Headless runs cannot ask.** The `question` tool would block an
  `opencode run` process; the skill must emit a batch as text instead.
- **Repo vs. plan drift.** Approval binds to file digests; implementation
  refuses to start when the spec files are dirty against the approved digest.

## Future steps

1. **Try before writing.** Copy aiskills' `grill-with-docs` skill into
   `~/.config/opencode/skills/`, adapt the frontmatter to OpenCode's format,
   and run it by hand on one real project. No Command Center code involved.
2. **Judge it on signal, not vibes:** did it surface something you had not
   considered; did the batch/default format suit your time budget; did it know
   when to stop.
3. **Then decide:** vendor it with attribution (Apache-2.0, keep a NOTICE), or
   write a leaner skill carrying our contracts (no `question` tool, batch +
   defaults, ledger, dry-round exit, four-word verdicts).
4. **Write the four templates** (or adapt aiskills' SPEC / DESIGN / ADR /
   CONTEXT templates) so artifacts have a fixed shape before the digest gate
   exists.
5. **Wire into the Shape call-site:** `plan` + `--file brief`, parse the
   question batch, write the artifacts, compute the digest, run the consistency
   audit.
6. **Update `SPEC.md`** §5 with the mechanic and add a Prior art section
   recording licenses and what was taken.
7. **Promote in the store** when the skill has been tested by hand and the shape is
   settled.

## Decision log

| Decision | Why |
|---|---|
| Three call-sites, two stock agents | The grill does not need its own agent; `plan` is already read-only and shell-capable. The 30-agent version is the thing we are avoiding. |
| Skill, not code | Behaves the same interactively and headlessly; no rebuild to tune the questions. |
| Batch with defaults, not one-at-a-time | The operator's time is the scarce resource. Every question should be answerable with one keystroke. |
| Four artifacts, one digest | Decisions and vocabulary are part of the contract, not commentary. |
| Try an existing skill before writing one | Cheapest possible validation of the mechanic. |
