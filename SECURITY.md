# Security and Trust Boundaries

*Status: v5, 2026-10-26. Accepted risks for a local, single-user system. This is
a register, not a promise of isolation.*

## 1. Threat model

The design protects against **accidents, drift, and self-inflicted damage**: a
run that touches the wrong file, a gate that was weakened, a push that should not
happen, an approval that skipped the evidence.

It does **not** contain hostile code or a determined adversary with local access.
Trusted by assumption: the operator, the workstation, the repositories being executed,
and the LAN.

## 2. Agent runtime

All four agents (`cc-inquisitor`, `cc-coder`, `cc-reviewer`, `cc-chat`) run as the
operator's user through OpenCode on the same machine. P4 installs `cc-chat` but
does not add a chat UI.

- The permission table in `SPEC.md` §4.1 is a **guardrail, not a sandbox**.
- A shell-capable agent can read environment variables and files outside the
  worktree, write outside the intended scope, and make network requests.
- The read-deny rules for `.env*` and `~/.ssh/**` apply to the read tool; they do
  not stop `cat`, `env`, or `curl` from a shell.
- Prompt injection is possible through repository content, dependencies,
  READMEs, commit messages, and issue text.

Consequence: run Command Center only on repositories and branches you trust.
Review the diff, not just the gate.

## 3. Secrets

- Provider API keys, the Command Center bearer token, and local model
  credentials are available to the daemon and therefore potentially to an agent
  process.
- A hostile or injected run could read and exfiltrate them.
- Secrets are never committed. `.env*` is excluded from the read/edit tools.
- Provider keys live in `~/.config/command-center/env` (mode 600), which the
  systemd unit loads as an `EnvironmentFile`. The daemon passes its environment
  to agent processes, so that file holds credentials only — never the PIN.
- The PIN lives only in `config.yaml`, which is ignored; every other file —
  tests, docs, fixtures, commit messages — uses a random value.
- `.githooks/` refuses a commit that carries a local config file, the live PIN,
  a key-shaped string, or — outside `tests/` — any `pin:` value other than the
  `0000` placeholder. That last rule needs no secret, so it also holds in CI
  and in a fresh clone. Enable it once per clone:
  `git config core.hooksPath .githooks`.
- If a run behaves unexpectedly, rotate the provider key and the CC token.

## 4. Git

- The machine never pushes. This is enforced by a per-worktree
  `core.hooksPath` pre-push hook and the agent shell deny list for
  `git push`, `git commit`, `git remote`, and `git config`.
- Those are **mistake guards**, not a security boundary against a determined
  process. The operator merges `cc/integration` to master and pushes.
- The `remote.*.pushurl` trick is deliberately not used: it is repo-shared and
  would break the operator's own pushes.

## 4.1 Auto-merge

- A task that passes the gate, policy scan, and independent reviewer
  **auto-merges into `cc/integration`** without human approval.
- Command Center ends at `cc/integration`. Moving it to master is a git merge
  the operator runs outside the tool, and the operator pushes.
- A bad task therefore sits in the base that later tasks build on. The correction
  path is `git revert <merge_sha>` on integration, then re-run or rebase the
  affected tasks. Reverting a landed task is more expensive than rejecting a
  single one.
- The gate and policy are deterministic; the reviewer is an independent model
  session. None of them is a human diff review.
- Run only trusted repositories. The policy scan is detection, not a sandbox.

## 4.2 Native cockpit

- The PySide6 cockpit reads the same bearer token from
  `~/.config/command-center/token`; it introduces no second secret.
- Settings is read-only: it exposes configured paths, roles, budgets, policy,
  and model availability, but never the token value or environment-key values.
- Live logs and batch diffs may contain source code and test output. They stay
  on the local daemon/desktop unless the operator exposes the API on the LAN.
- Tray notifications display project/run ids and failure reasons on the local
  desktop. Nothing in the cockpit merges anything.
- The `.desktop` launcher uses an absolute path to this repo's venv command.
  That path is machine-specific and must be set in your own config.

## 4.3 Shaping

- `cc-inquisitor` has read access to the whole checkout but write access only
  under `docs/spec/**`; shell, network, and subagent spawning are denied.
- Shape worktrees are isolated from the operator checkout. The harness owns
  commits, store materialization, the gate file, and the integration merge.
- Store tasks exist while the spec phase is `shape`; they are not eligible to
  build until `cc shape approve`.
- A rejected or cancelled shape leaves materialized store tasks open and
  droppable. It restores the previous `spec_state`.
- Shape approval merges to `cc/integration`, not master; the operator merges
  and pushes by hand.

## 5. Network and LAN

- The Command Center daemon binds `127.0.0.1` by default. `0.0.0.0` is an
  explicit opt-in.
- LAN access uses the master bearer token for CLI/cockpit and a browser PIN
  exchange. The configured PIN (4–8 digits) is weaker than the master token:
  five failures per IP per minute are rejected, and the session token lives
  only in daemon memory. There is no TLS in v1; plaintext HTTP on a trusted LAN
  is an accepted risk. Do not port-forward it.
- The browser stores a session token in `localStorage`, not the PIN or the
  master token.
- The native cockpit is a local X11/Wayland client; it does not open a port.
- The task store is assumed unauthenticated and listens on
  `0.0.0.0:4180`. Keep it on the trusted LAN. Tailscale can front either service
  later.
- The browser stores the bearer token in `localStorage`. The web shell is served
  without auth; no data is returned without the token. The `EventSource` URL
  carries `?token=`, which can appear in browser history and proxy/access logs.
- Approval actions are audited with origin IP, timestamp, and reason. v1 has
  one shared token, so there is no per-client token id.

## 6. Filesystem

- The intended write scope for a run is the task worktree plus its run directory.
- The edit tool is scoped to those paths. The shell is not.
- Command Center state (SQLite, snapshots, run artifacts) is owned by the daemon;
  clients never write it.
- `docs/spec/**` and `command-center.yaml` are immutable during Build and checked
  byte-for-byte against `cc/integration`.

## 7. When this changes

Stop and add a real sandbox **before** any of these:

- running on an untrusted repository or a public PR;
- executing third-party code or unreviewed dependencies;
- exposing Command Center beyond the trusted LAN;
- adding a second user or a hosted service.

## 8. Hardening backlog

1. `bwrap`/`firejail` wrapper for agents: read-only home, writable worktree and
   run dir, no provider key in the child environment.
2. Egress allowlist for the agent shell (package registries only), or a proxy.
3. Token-gate the task store.
4. TLS or Tailscale for Command Center LAN access.
5. Per-run environment scrub for gates and agents.
6. Shell-pattern deny list as defense in depth — useful, never sufficient.
