# Configuration and local storage

Configuration loads through Pydantic Settings. Precedence is CLI overrides, environment,
the selected repository's review options, user TOML, then defaults. The repository section
is selected with `doctor --repo acme/api`; configuration is never read from repository code.
No dotenv file is loaded. Unknown TOML keys and invalid values are errors. Diagnostics
include keys and reasons without echoing input values or database credentials.

The default file is `$XDG_CONFIG_HOME/diffbutler/config.toml`, falling back to
`~/.config/diffbutler/config.toml`. `doctor --config FILE` chooses an explicit file.
An empty file is valid. No forge or reviewer is configured by default.

For GitHub configuration and doctor authentication checks, see the
[GitHub adapter guide](github.md). `doctor --offline` skips forge checks and credential
resolution while retaining local configuration, database, and path checks.

```toml
[general]
poll_interval_s = 180
max_concurrent_reviews = 2

[db]
auto_migrate = true
# Omit url to use $XDG_DATA_HOME/diffbutler/diffbutler.db
# (default ~/.local/share/diffbutler/diffbutler.db).
# url = "sqlite+aiosqlite:///~/my-data/diffbutler.db"

[review]
mode = "source_only"
checkout = "worktree"
findings_threshold = "minor"
min_confidence = 0.5

[repos."acme/api"]
enabled = false
path = "~/src/acme/api"
checkout = "mediated"
```

These workflow settings are validated now; manual queue sync is implemented, while
periodic polling and AI review workflows are not yet implemented. `enabled` is
repository AI opt-in metadata, not a trigger to start anything.
The full planned example in [SPEC.md §11](../SPEC.md#11-configuration) also parses;
M0 supports configuration for CLI reviewers only. Reviewer IDs must be unique and
`default_reviewer` must name a configured reviewer. Repo `default_reviewer`, `checkout`,
and `prompt` override the global review section when that repository is selected.
Repository and prompt paths expand `~`; relative paths resolve beside the config file.
Relative SQLite URL paths resolve against the process working directory.

Environment variables use `DIFFBUTLER_` and `__` between nested keys:

```sh
DIFFBUTLER_GENERAL__POLL_INTERVAL_S=300 uv run diffbutler doctor
DIFFBUTLER_DB__AUTO_MIGRATE=false uv run diffbutler doctor
uv run diffbutler doctor --db-url sqlite+aiosqlite:////tmp/diffbutler.db
```

Lists/objects use JSON environment values. `--db-url` overrides the environment;
`--no-migrate` overrides `db.auto_migrate` to false. Configuration loading itself creates
no files and performs no forge, subprocess, or model operations.

## Database initialization

M0 supports persistent local SQLite files through `sqlite+aiosqlite`. PostgreSQL/MySQL
drivers, remote URLs, SQLite URI query options, and in-memory databases are not supported
yet. Connections enable foreign keys, WAL journaling, and a five-second busy timeout.

Doctor applies packaged Alembic migrations by default. The initial revision creates
`account`, `repository`, `pull_request`, `review_task`, and `cleanup_job`, including a
stable local account and per-account review-cycle identity. Revision `0002` adds
`forge_account` (API instance/login mapping and optimistic sync generation),
`pr_monitor` (following, request provenance, per-account PR snapshot and activity
watermarks), and the task's completion SHA. Known older revisions upgrade with a
backup; the shipped `0001` is unchanged.

Discovery/follow persist pending attention. Observed completion retains cycle history
and atomically schedules a cleanup job. Monitor state survives completed cycles and
request removal. No cleanup worker or history interface is implemented. Revision `0003`
adds session identity and analysis-attempt tables, including task/reviewer
foreign-key consistency and unique analysis-key/attempt pairs. No sessions or runs
are created by queue sync. Revision `0004` adds durable worker claims for the internal
[explicit scheduling service](review-scheduling.md). Revision `0005` adds a per-run
`review_result` record for orientation/all findings, parse diagnostics, and threshold
selection; raw output remains on `ai_run`. Execution, foreground workspace
ownership, and cleanup remain unimplemented; per-finding disposition and posting tables are deferred.
HTTP response validators remain in memory; restart reads are unconditional.

Existing databases receive a consistent SQLite backup before a pending migration.
Backups include committed WAL contents and live beside the database as
`DATABASE.TIMESTAMP.UNIQUE_ID.bak`. Backup failure stops migration. An already-current
database is not backed up again. Startup migrations serialize with a SQLite write lock;
unknown revisions and unversioned tables are rejected rather than adopted or overwritten.
Backups are retained for the user to manage; they may contain the same private data as
the database and must not be committed.

With `db.auto_migrate = false` or `doctor --no-migrate`, a missing database or stale
schema fails without applying migrations. This is a schema check, not a filesystem
read-only mode: connections still configure SQLite pragmas and can create WAL sidecars.
The migration round-trip and model/schema comparison tests run offline against SQLite.
Other database backends and their CI matrix remain deferred.

Revision `0006` retains [anchor validation outcomes](diff-anchors.md) independently
of model findings; older results remain explicitly unvalidated.
