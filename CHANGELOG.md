# Changelog

## Unreleased

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
