# Agent instructions

## Start here

- Read `CONTRIBUTING.md` before changing this repository and the relevant sections
  of `SPEC.md` before changing workflow or architecture.
- `SPEC.md` describes the target design. Check the code and README for what is
  implemented; do not describe planned functionality as shipped.
- Keep changes focused on the requested task. Add runtime dependencies only when
  used, with `uv add`; update both `pyproject.toml` and `uv.lock` and review licenses
  using `docs/dependencies.md`.

## Architecture

- Keep `domain/` pure Python without I/O. Keep `core/` async and UI-independent,
  using protocols for forge, reviewer, and store dependencies.
- Both CLI and TUI use the shared core. Do not put workflow logic in UI handlers
  or expose ORM objects across the core API boundary.
- Keep terminal rendering in the UI and Textual imports inside `ui/tui/`.
  Preserve the import-linter contracts in `pyproject.toml`.
- Use async I/O for long operations and subprocess argv execution, never shell
  interpolation of PR titles, branches, or other untrusted input.

## Workflow invariants

- Every background AI run requires an explicit user action. Discovery, pushes,
  renewed requests, and session return must never automatically start reviews.
- Background analysis uses the configured agent CLI's native read-only sandbox
  and approval controls; DiffButler does not provide an additional OS sandbox. Foreground posting requires explicit human instruction; entering a
  session is not permission to post. A clean AI pass is not approval.
- Reconcile actual forge activity rather than trusting agent claims of posting.
  Keep request membership separate from followed-PR monitoring.
- Completion removes the active task and durably schedules cleanup. Delete only
  session-owned resources, preserve review history, and defer workspace deletion
  while a foreground agent owns it.
- Preserve terminal restoration on exit, crash, and launch failure. Background
  jobs must not write to a terminal owned by an interactive session.
- Source-only restrictions in the spec apply to DiffButler's review processes.
  Agents developing DiffButler should run its tests and development checks.

## Validation

- Use Python 3.14+, uv, and the existing Make targets. `make setup` prepares the
  locked environment; `make` lists available commands.
- For code changes, run `make format` and `make check` (Ruff, strict Pyright,
  import boundaries, and tests). Run `make smoke` for packaging or entry-point
  changes. Report checks that could not run and why.
- Add meaningful tests for behavior changes, especially lifecycle transitions,
  reconciliation, isolation, and cleanup. Prefer fakes for forge/reviewer services.
- Tests must be offline: no real forge calls, credentials, or paid model requests.
- Keep documentation accurate when behavior changes. Documentation-only changes
  need a content/link review rather than a full test run.
- Never commit credentials, private PR content, databases, session transcripts,
  virtual environments, or generated build artifacts.

## Local commits

- Make focused local commits for completed, validated changes when committing is
  within the user's requested scope. Inspect the diff and staged files first;
  include only intended changes and preserve unrelated work.
- Every agent-created commit must include a `Co-Authored-By: Name <email>` trailer,
  separated from the message body by a blank line, identifying the assisting agent.
  For Codex, use `Co-Authored-By: Codex <noreply@openai.com>`.
- Preserve the user's configured Git author identity. Do not invent a human
  identity or change global Git configuration to make a commit succeed.
- Use a concise commit subject describing the change. Local commit permission
  does not imply permission to push or publish.
