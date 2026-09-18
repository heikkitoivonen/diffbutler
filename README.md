# DiffButler

A terminal dashboard for human-directed AI code reviews.

DiffButler is being built to keep track of what each pull request needs next:
AI review, your review, or author changes. You decide when an AI review starts,
discuss findings in the agent's own terminal session, and do the human pass in
your browser.

**Status: M0–M2 implemented for the Linux/Codex scope below. The dashboard is M3.**
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
single refresh; there is no periodic poller yet. These commands never invoke AI or
post to a forge. [Native reviews and resumable sessions](docs/native-reviews.md) are
available with a configured Codex reviewer and pinned local Git objects:

```sh
diffbutler review 'acme/api#1841'
diffbutler findings 'acme/api#1841'
diffbutler session 'acme/api#1841'
diffbutler approved 'acme/api#1841'
```

A review sends redacted source to the configured agent/model provider only after the
explicit review command. DiffButler relies on the agent's native read-only sandbox;
it does not require Bubblewrap or add its own OS sandbox. Current version support,
authentication, source limits, and validation evidence are in the [native review guide](docs/native-reviews.md).
Claude and macOS integration remain deferred. Tests are offline; no live managed
review was invoked during implementation.

The shared core provides [review scheduling](docs/review-scheduling.md),
[pinned source preparation](docs/source-context.md), [output parsing](docs/review-parsing.md),
[anchor validation](docs/diff-anchors.md), and [durable session cleanup](docs/session-lifecycle.md).
The [terminal handoff experiment](docs/terminal-handoff.md) remains independently runnable
with `make handoff`. The [Bubblewrap experiment](docs/background-isolation.md) is legacy
optional work, not a prerequisite for native reviews.

## Local setup and diagnostics

```sh
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
`doctor --config FILE` requires that file to exist. For settings, examples, environment
overrides, and database backup behavior, see [configuration and storage](docs/configuration.md).

## Planned workflow

- Include review requests addressed to you or your teams, with an all-PR browser.
- Press `r` to start a background, source-only AI review. Reviews never start automatically.
- Press `v` to hand the terminal to the resumed agent for discussion and instructed posting.
- Return to a dashboard refreshed from GitHub. Press `o` for the manual browser review,
  or `w` to mark that you're waiting for author changes.
- Mark approved with `m`, or let closure/merge finish the task. Clean up the session
  automatically and retain review history in SQLite.

Background analysis will be read-only. Interactive agents may post only on your
instruction. Source sent to an AI agent may leave your machine for its model provider;
repository opt-in and credential isolation are part of the design.

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
The Linux/Codex native adapter connects these services through the shared core and CLI.
The optional Bubblewrap primitive is not part of that execution path.
The terminal handoff experiment lives in `ui/tui/`.
Import-linter enforces their dependency direction from the start. Textual is used by
the experiment; Pydantic validates configuration, SQLAlchemy/Alembic manage SQLite,
HTTPX provides async GitHub reads, and Typer powers the CLI.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
[SECURITY.md](SECURITY.md). The current design and milestones are in [SPEC.md](SPEC.md).
Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Dependency licensing notes are in [docs/dependencies.md](docs/dependencies.md).
