# Configuration and local storage

Configuration loads through Pydantic Settings. Precedence is CLI overrides, environment,
the selected repository's review options, user TOML, then defaults. The repository section
is selected with `doctor --repo acme/api`; configuration is never read from repository code.
No dotenv file is loaded. Unknown TOML keys and invalid values are errors. Diagnostics
include keys and reasons without echoing input values or database credentials.

The default file is `$XDG_CONFIG_HOME/diffbutler/config.toml`, falling back to
`~/.config/diffbutler/config.toml`. `doctor --config FILE` chooses an explicit file.
An empty file is valid. No forge or reviewer is configured by default.

Run `diffbutler config` to show the file and its full path. If missing, the command
creates the parent directories and writes an editable default configuration first.
`diffbutler config --config FILE` does the same for a custom path, including one that
does not yet exist. Existing files are displayed without validation or rewriting,
so comments and invalid TOML remain available for inspection. The output is the
file's contents, not the effective configuration after environment overrides.
Generated defaults include commented setup examples; data paths remain unset so
they continue to follow `XDG_DATA_HOME`. This command does not initialize a database,
contact a forge, or launch an agent.

Use `diffbutler config --edit` to create the file if missing and open it in `$EDITOR`
instead of displaying its contents. This also accepts `--config FILE`. DiffButler
waits for the editor to exit and then prints the file path. Set `EDITOR` to an
editor command, such as `vim` or `code --wait`; quoted arguments are supported and
the command runs without a shell. An unset/empty `EDITOR`, launch failure, or
unsuccessful editor exit is reported as an error.

## First-run setup

To start over, close running DiffButler sessions and run `diffbutler reset`.
It prints and removes both `$XDG_CONFIG_HOME/diffbutler` (default
`~/.config/diffbutler`) and `$XDG_DATA_HOME/diffbutler` (default
`~/.local/share/diffbutler`), including configs, databases, history, and session
files. Confirmation defaults to **No**; `diffbutler reset --force` skips it.
Missing directories are harmless, and invalid configuration does not block reset.
Custom paths outside these directories are preserved. Symbolic links are removed
without deleting their targets.

Starting the live dashboard (`diffbutler` in a terminal or `diffbutler dashboard`)
with no configured forge runs setup before rendering. It inspects local Git metadata
without fetching. A GitHub `origin` remote supplies the forge and `owner/repo`; without
`origin`, all remotes must agree. HTTPS and SSH remote forms are supported, and running
from a repository subdirectory saves the Git root as its local source path. Credentials
embedded in remote URLs are never saved. Ambiguous or missing information prompts for
the forge and repository; unknown hosts require confirmation as GitHub Enterprise.
GitLab is not implemented. Existing forge configurations skip setup.

Prepare authentication before launching: prefer
`export GITHUB_TOKEN="$(gh auth token)"` with an existing `gh` login, or create a
[fine-grained token](https://github.com/settings/personal-access-tokens) with
**Pull requests: Read-only** permission for the selected repositories. The
[authentication guide](github.md#authentication-options) covers host/account options,
Git transport, and the anonymous fallback, which is not recommended because of its
low API quota.

Setup checks repository visibility with an anonymous API request, then asks for your
GitHub username, defaulting to the owner part of `owner/repo`. For an organization-owned
repository, replace that default with your personal username. For public repositories,
choose `token` (recommended and the default) or explicitly choose `anonymous`.
Anonymous access omits `token_env`, saves `login`, and monitors only direct requests
without resolving any token. Public-repository setup disables team-request discovery
in either mode.

For token access, including private or anonymously inaccessible repositories, setup asks for the token's
**environment variable name**, defaulting to `GITHUB_TOKEN`. Set that variable before
launching DiffButler. Setup validates the token's account against the chosen username,
repository access, and pull-request read permission. An unset variable, invalid token,
account mismatch, or inaccessible repository exits with an error before configuration
is saved. Rate-limit/network errors also abort setup; they are not treated as evidence
that a repository is private. Only the variable name is saved, never its value.
Existing anonymous configurations stay anonymous until you add `token_env = "GITHUB_TOKEN"`
to their `[[forges]]` entry. For a minimal fine-grained token, also set
`include_team_requests = false`; private-repository setup otherwise enables team discovery.

The selected forge gets `repositories = ["owner/repo"]`, which limits both direct and
team review-request discovery. An empty list (the default for manually configured
forges) searches across accessible repositories. Each entry must be `owner/repo`.
Explicitly followed PRs continue to be monitored independently of this request filter.
The chosen repository's existing AI opt-in and review settings are preserved.

Setup saves to the default config or the dashboard's `--config FILE`, creating it if
missing and preserving unrelated settings/comments. It does not save environment
overrides or prompt for token values. Existing authenticated configurations continue
to support an environment token or `gh` login. No AI review starts automatically. Offline dashboards,
help/version, and other CLI commands do not run setup. Without an interactive terminal,
incomplete detection reports an error rather than prompting. Cancelled prompts do not
save partial setup.

For GitHub configuration and doctor authentication checks, see the
[GitHub adapter guide](github.md). `doctor --offline` skips forge checks and credential
resolution while retaining local configuration, database, and path checks.

```toml
[general]
# Omit for 60 seconds with authenticated accounts, 600 with any public account.
# poll_interval_s = 600 # Explicit override for all accounts.
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

These settings configure manual sync, dashboard polling, and explicit native reviews. `enabled` is
repository AI opt-in metadata, not a trigger to start anything.
Configuration supports CLI reviewers only. Reviewer IDs must be unique and
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

DiffButler supports persistent local SQLite files through `sqlite+aiosqlite`. PostgreSQL/MySQL
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
request removal. Completion cleanup and the [offline history interface](dashboard.md) are implemented. Revision `0003`
adds session identity and analysis-attempt tables, including task/reviewer
foreign-key consistency and unique analysis-key/attempt pairs. No sessions or runs
are created by queue sync. Revision `0004` adds durable worker claims for the internal
[explicit scheduling service](review-scheduling.md). Revision `0005` adds a per-run
`review_result` record for orientation/all findings, parse diagnostics, and threshold
selection; raw output remains on `ai_run`. Native execution, foreground ownership, and cleanup are described in the
[native review guide](native-reviews.md); per-finding disposition and posting tables are deferred.
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

Revision `0007` adds durable [foreground leases and cleanup claims](session-lifecycle.md).

Revision `0008` retains claim-protected launch intent and host/boot identities for recovery.


Revision `0009` adds retained forge receipts, per-cycle PR snapshots, refresh-pending
status and persistent UI preferences. See [dashboard operation](dashboard.md).

`ui.columns` and `ui.sort_by` accept the registered IDs listed in SPEC §10.2;
unknown IDs fail validation. `ui.browser_command` accepts an argv list, for example
`["firefox", "--new-tab"]`; the UI appends the PR URL without a shell. The dashboard
persists interactive sort and AI-dispatch pause choices in SQLite. It supports active,
requested, following, triage, posted/waiting, and history presets; `all` opens accessible
PR search. The future snoozed preset is not yet a separate dashboard view.

The dashboard's `a` switch enables monitoring all open PRs in configured repositories.
This preference is stored in the local database, not the TOML file, and survives restarts.
Every forge needs a nonempty `repositories` list to enable it. The default is review-request
discovery only. Existing followed PRs continue to be monitored when it is disabled.
