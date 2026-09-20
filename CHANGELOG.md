# Changelog

## Unreleased

- Add the dashboard `a` switch to persistently monitor all open PRs in configured
  repositories, with request membership kept separate and no automatic AI runs.

- Check repository visibility during first-run setup. Ask for a GitHub username
  defaulting to the repository owner; use anonymous direct-request monitoring for
  public repositories, or validate a named token environment variable before saving
  private-repository configuration. Invalid credentials exit without saving secrets.
- Configure the first live dashboard launch from the current GitHub remote, or prompt
  for the forge and repository. Preserve existing TOML and scope direct/team review
  requests with `forges.repositories`; no AI review starts automatically.
- Add `diffbutler config --edit` to open the selected config file in `$EDITOR`.
- Add `diffbutler config [--config FILE]` to show configuration, create defaults
  when the file is missing, and print its path for editing.
- Support `-h` as an alias for `--help`, including subcommands, and `-v` for `--version`.

- Remove the custom Bubblewrap runner, its probe fixtures, kernel-isolation CI job,
  Make target, and documentation. Agent-native sandbox and approval flags remain in use.

- Default the dashboard and terminal handoff experiment to the `ansi-dark` theme.

- Remove Textual's built-in screenshot command and action from the dashboard and
  terminal handoff experiment.

- Complete the Linux/Codex M3 native dashboard loop with explicit reviews, same-terminal
  discussion, forge refresh on return, manual/waiting overrides, completion cleanup and
  offline cycle history. Add serial polling, accessible-PR search, configurable sorting,
  pinned diff/log views, browser fallback and persistent AI-dispatch pause.
- Retain and deduplicate submitted review/inline/discussion receipts in migration `0009`,
  preserve manual choices against older refreshes, and display incomplete refresh state.
- Validate synthetic review → discussion/feedback → cleanup/history and repeated production
  dashboard handoff in a controlling PTY. Live managed-provider acceptance remains unperformed.

- Complete Linux/Codex M2 offline acceptance through the public CLI, including
  explicit retry, exact-session terminal resume after restart, completion cleanup,
  and retained findings. Live managed-provider acceptance remains unperformed.
- Move configured foreground ownership into the shared core, show cleanup job
  status in CLI results, and checkpoint cleanup intent for verified reboot recovery.

- Add the Linux/Codex M2 native path: explicit CLI review, pinned redacted
  snapshots, bounded JSONL capture, exact-session resume, local completion and
  automatic owned-home cleanup with retained findings. Use agent-native sandboxing;
  Codex 0.98.0 flags were checked locally,
  and lifecycle acceptance is offline; Claude/macOS integration remains deferred.

- Add durable pre-launch metadata in migration `0008`, PID checkpoints, complete
  ownership listings, and conservative recovery after a verified local reboot.

- Add claim-protected resource registration before creation, private managed directories,
  confined idempotent filesystem cleanup, and cancellation-safe filesystem operations.
  Native backend deletion remains unsupported unless an adapter is supplied.

- Add versioned local prompt serialization with precision-first source-only instructions,
  parser-derived schema, untrusted JSON framing, pre-escape redaction, and byte limits.

- Add internal requested-run worker orchestration, live revision/policy checks,
  backend-ID checkpoints, and exit-before-finalization ordering under cancellation.
  Tests use offline executions; no production reviewer factory is connected.

- Add durable foreground ownership and a completion-cleanup worker step with migration
  `0007`, bounded retries, cancellation-safe ownership, and retained analysis history.
  Native launch, process recovery, and deletion adapters remain disconnected.

- Add declarative offline envelope/JSONL extraction, strict completion/error handling,
  raw-capture preservation, and transactional extracted-session checkpoints.

- Add side-aware unified-diff indexing before redaction, conservative range snapping,
  and retained original/exact/snapped/demoted anchor outcomes in migration `0006`.
  Findings are never dropped because their anchors are invalid.

- Add claim-protected result finalization and migration `0005`: persist orientation,
  all findings, captured output, parse diagnostics, and threshold selection. Route
  current attempts to human review or triage without letting late results replace
  newer attention. Result reads survive restart; real execution remains disconnected.

- Add internal captured review output parsing with required orientation, immutable
  findings, tiered JSON recovery, bounded input, and safe failure diagnostics. Raw
  output and all valid findings are retained. Backend execution and anchor
  validation remain deferred.

- Add internal review context assembly with revision checks, bounded pinned Git
  diffs, mediated surrounding source, and explicit context limitations. Unsupported
  binary/non-UTF-8 diffs fail preparation. Reviewer execution remains disconnected
  pending redaction and enforced process isolation.

- Add internal explicit review requests and transactional worker claims, duplicate-start
  suppression, deliberate retry attempts, and migration `0004`. Sync invalidates obsolete
  runs atomically; cancelled workers retain ownership until exit is acknowledged.
  Real reviewer execution and process isolation remain deferred.

- Add M2 reviewer/source protocols, immutable session/run DTOs, and migration `0003`
  for durable conversation identity and unique analysis attempts. Execution and
  session management remain deferred; queue operations still never start AI.

- Add persistent account-scoped review queues, independent followed-PR refresh,
  request provenance, restart-safe activity watermarks, and duplicate-event suppression.
- Observe own approvals and closure/merge, retaining cycle history and atomically
  scheduling cleanup; later pushes or renewed requests create pending attention.
- Add migration `0002` and backed-up upgrades from known older schema revisions.
- Replace argparse with Typer; expose `sync`, local queue listing, paginated all-PR
  browsing, and `follow`, including JSON output and partial-coverage warnings.

- Add the GitHub read adapter: direct/team request discovery with provenance and
  incomplete-coverage reporting, paginated search, and host-scoped PR lookup.
- Add bounded async HTTP reads, conditional responses, quota/cooldown handling,
  and synthetic offline tests for authentication and credential confinement.
- Check configured GitHub accounts in doctor; add optional expected-login matching
  and `--offline` for local-only diagnostics.

- Load validated TOML/environment configuration with repository and CLI overrides.
- Add the async SQLite ledger and packaged initial Alembic migration, local account,
  and consistent backups before upgrading existing databases.
- Add local `diffbutler doctor` diagnostics, including no-forge initialization and
  explicit skips for deferred forge/reviewer checks.

- Add an offline Textual handoff experiment and real pseudo-terminal tests for
  foreground ownership, Ctrl+C, crashes, repeated resume, and background progress.
- Record the provisional same-terminal decision and remaining native/backend checks.
- Confirm Linux/Codex continuity manually; cover resize and a full-screen child
  in pseudo-terminal tests and record environment details in probe diagnostics.

- Initialize the Python 3.14 package and CLI scaffold with uv.
- Add Ruff, strict Pyright, pytest, coverage, import boundaries, and dependency auditing.
- Add Makefile commands, GitHub CI, community documentation, and the MIT license.
- Record the DiffButler workflow and architecture in the design specification.
