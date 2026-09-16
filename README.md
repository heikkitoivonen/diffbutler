# DiffButler

A terminal dashboard for human-directed AI code reviews.

DiffButler is being built to keep track of what each pull request needs next:
AI review, your review, or author changes. You decide when an AI review starts,
discuss findings in the agent's own terminal session, and do the human pass in
your browser.

**Status: project scaffold, not a working review dashboard yet.** The CLI currently
provides help and version information. GitHub polling, agent sessions, SQLite
history, and automatic cleanup are planned in [SPEC.md](SPEC.md).

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

The layer packages are placeholders. Import-linter enforces their dependency direction
from the start. Runtime dependencies will be added with the features that need them.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md), [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
[SECURITY.md](SECURITY.md). The current design and milestones are in [SPEC.md](SPEC.md).
Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE). Dependency licensing notes are in [docs/dependencies.md](docs/dependencies.md).
