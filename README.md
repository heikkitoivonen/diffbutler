# DiffButler

A terminal dashboard for human-directed AI code reviews.

DiffButler is being built to keep track of what each pull request needs next:
AI review, your review, or author changes. You decide when an AI review starts,
discuss findings in the agent's own terminal session, and do the human pass in
your browser.

**Status: M0–M3 implemented for the Linux/Codex scope below, with offline acceptance.**
Configuration, SQLite migrations, doctor, and the [GitHub adapter](docs/github.md)
now support a persistent review queue through the Typer CLI. Direct/team requests
are deduplicated, followed PRs remain monitored after request removal, and observed
own approval or closure completes a cycle with a durable cleanup job.

```sh
uv run diffbutler sync
uv run diffbutler ls
uv run diffbutler ls --view requested
uv run diffbutler ls --view following
uv run diffbutler ls --view all --repo acme/api
uv run diffbutler follow 'acme/api#1841'
```

Configure a forge before remote commands. Local listing works offline. `sync` is a
single refresh; the dashboard polls periodically. These commands never invoke AI or
post to a forge. [Native reviews and resumable sessions](docs/native-reviews.md) are
available with a configured Codex or Claude reviewer and pinned local Git objects:

```sh
diffbutler review 'acme/api#1841'
diffbutler findings 'acme/api#1841'
diffbutler session 'acme/api#1841'
diffbutler approved 'acme/api#1841'
```

A review sends redacted source to the configured agent/model provider only after the
explicit review command. DiffButler relies on the agent's native read-only controls;
it does not add its own OS sandbox. Current version support,
authentication, source limits, and validation evidence are in the [native review guide](docs/native-reviews.md).
The presets target Codex CLI 0.155.1 and Claude Code 2.1.278 on Linux; macOS remains
deferred. Tests are offline; no live managed review was invoked during implementation.

The shared core provides [review scheduling](docs/review-scheduling.md),
[pinned source preparation](docs/source-context.md), [output parsing](docs/review-parsing.md),
[anchor validation](docs/diff-anchors.md), and [durable session cleanup](docs/session-lifecycle.md).
The [terminal handoff experiment](docs/terminal-handoff.md) remains independently runnable
with `make handoff`.

## Local setup and diagnostics

Use `diffbutler -h` (or `--help`) for help and `diffbutler -v` (or `--version`)
for the installed version. Subcommands also accept `-h`, such as `diffbutler review -h`.

```sh
uv run diffbutler config
uv run diffbutler doctor
uv run diffbutler doctor --no-migrate
uv run diffbutler doctor --offline
```

With no forge configured, no credentials or agent installation is required. Doctor validates configuration,
initializes/checks SQLite, checks configured repository/prompt paths, and resolves the
platform browser opener without launching it. Failed checks exit with status 1; invalid
CLI arguments exit with status 2. Missing browser support is a warning. Configured GitHub
accounts receive read-only authentication, quota, and team visibility checks; use `--offline`
to skip them. Reviewer execution, resume, and cleanup are exercised by offline acceptance
tests; doctor never launches a model or posts to a forge.

Configuration lives at `$XDG_CONFIG_HOME/diffbutler/config.toml` (default
`~/.config/diffbutler/config.toml`); a missing default file uses safe defaults.
`diffbutler config` displays the file, creating it with defaults if missing, and
prints its full path at the end. Use `diffbutler config --config FILE` for another
location. Existing contents and comments are preserved.
Add `--edit` to open the file in `$EDITOR`, creating it first if missing.
`diffbutler reset` removes the standard DiffButler config, data, and state directories,
including databases, saved sessions, and diagnostic logs, after confirmation. Use `diffbutler reset --force`
to skip confirmation. Custom files outside those directories are preserved.
`doctor --config FILE` requires that file to exist. For settings, examples, environment
overrides, and database backup behavior, see [configuration and storage](docs/configuration.md).

## Dashboard workflow

Run `diffbutler dashboard` (or `diffbutler` in an interactive terminal). Use
`diffbutler dashboard --offline` for local queue/history reads without polling.
See the [dashboard guide](docs/dashboard.md) for keys, configuration, and acceptance evidence.

On first launch with no configured forge, DiffButler detects GitHub from the current
Git repository's `origin` remote (or a single unambiguous remote). It saves the forge,
repository request filter, and local repository path. Otherwise it asks for the forge
and repository before opening the dashboard. Setup checks visibility and asks for your
GitHub username, defaulting to the repository owner. Public repositories use no token
and monitor direct review requests. Private repositories prompt for a token environment
variable name and validate its token before saving. Authenticated accounts can include
team requests. Setup does not start an AI review.

- Include review requests addressed to you or your teams, with accessible-PR search (`b`).
- Press `a` to toggle automatic monitoring of all open PRs in configured repositories.
- Press `r` to start a background, source-only AI review. Reviews never start automatically.
- Press `v` to hand the terminal to the resumed agent for discussion and instructed posting.
- Return to a dashboard refreshed from GitHub. Press `o` for the manual browser review,
  or `w` to mark that you're waiting for author changes.
- Mark approved with `m`, or let closure/merge finish the task. Clean up the session
  automatically and retain review history in SQLite (`h` or `diffbutler history`).

Background analysis is read-only. Interactive agents may post only on your
instruction. Source sent to an AI agent may leave your machine for its model provider;
repository opt-in and the native permission/authentication limitations are described above.

## Development quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) (0.12.13 or later
in the 0.12 series) and GNU Make. From a checkout:

```sh
make setup
make run ARGS="--help"
make check
```

`make setup` installs Python 3.14 if needed and creates `.venv` from the committed
`uv.lock`. You don't need to activate a virtual environment or install Node manually.
Linux and macOS are the initial development targets; Windows users can use WSL.

| Command | Purpose |
|---|---|
| `make` | List available commands |
| `make run ARGS="--version"` | Run the CLI |
| `make run ARGS="doctor"` | Validate local configuration and initialize/check SQLite |
| `make handoff` | Try dashboard → offline fake agent → dashboard in one terminal |
| `make format` | Apply safe lint fixes and format code |
| `make check` | Lint, formatting, strict typing, import boundaries, and tests |
| `make coverage` | Test with branch coverage and HTML/XML reports |
| `make smoke` | Build and test installation in a fresh environment |
| `make audit` | Check locked dependencies against vulnerability data; needs network |
| `make licenses` | Inspect installed dependency license metadata |
| `make upgrade` | Update locked dependencies within declared constraints |

For environments without Make, equivalent uv commands are listed in the [Makefile](Makefile).
The scaffold is not published; to install your local checkout as a tool:

```sh
uv tool install --python 3.14 .
diffbutler --help
```

## Project layout

```text
src/diffbutler/
  domain/       Pure entities and workflow rules
  core/         UI-independent application services
  forges/       GitHub and future forge adapters
  reviewers/    Background reviews and native agent sessions
  store/        Persistence
  config/       Configuration
  ui/cli/       Command-line interface
  ui/tui/       Dashboard and terminal handoff
tests/          Offline tests
scripts/        Development and packaging checks
```

The config, store, GitHub read adapter, doctor, and shared queue services are implemented;
M2 now includes reviewer/source protocols, immutable session/run DTOs, and durable
session/run tables, plus [explicit starts and transactional worker claims](docs/review-scheduling.md).
The Linux/Codex native adapter connects these services through the shared core, CLI and dashboard.
The terminal handoff experiment lives in `ui/tui/`.
Import-linter enforces their dependency direction from the start. Textual is used by
the dashboard and experiment; Pydantic validates configuration, SQLAlchemy/Alembic manage SQLite,
HTTPX provides async GitHub reads, and Typer powers the CLI.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
[SECURITY.md](SECURITY.md). The current design and milestones are in [SPEC.md](SPEC.md).
Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Dependency licensing notes are in [docs/dependencies.md](docs/dependencies.md).

### Diagnostic logging

Application diagnostics are written to `$XDG_STATE_HOME/diffbutler/logs/diffbutler.log`
(default `~/.local/state/diffbutler/logs/diffbutler.log`). Logs rotate at 5 MiB with
three backups. They include startup, dashboard sync/review events, agent process
start/exit, GitHub request timing and quota metadata, and failure tracebacks.
They exclude request URLs, bodies, authorization headers, prompts, and agent output.
Tracebacks omit source text, locals, and arbitrary exception messages; safe
configuration error messages are retained. Select a PR and press `L` for its separate
AI run output. Diagnostic logging writes only to files, keeping the terminal free.
`diffbutler reset` removes diagnostics along with configuration and data.
