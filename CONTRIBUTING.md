# Contributing to DiffButler

DiffButler is in alpha development. Read the [README](README.md) and relevant guides in
[docs/](docs/) before changing the workflow or architecture. Discuss larger changes
in an issue before implementing them.

## Set up and verify

Fork or clone the repository, install uv and GNU Make, then run:

```sh
make setup
make format
make check
make smoke
```

Python 3.14 is the minimum supported version. Ruff handles lint and formatting;
Pyright runs in strict mode. `make` lists individual checks, and
`make test ARGS="-k name"` runs selected tests. No shell activation is required.

Tests use pytest-xdist's CPU-based worker count (`-n auto`) by default, including
`make check`, `make coverage`, and direct pytest runs. Override the count with
`make test ARGS="-n 2"`, or run serially
with `make test ARGS="-n 0"` (also use `-n 0` for `--pdb` or uncaptured `-s` output).
Workers use work-stealing scheduling to balance tests with different durations.

CI runs the same checks and installation smoke test on Linux and macOS. Dependency
audits additionally run against public vulnerability data. Tests must run without
forge credentials, real GitHub requests, or paid model calls.

## Working conventions

- Keep the core independent of the terminal UI; run `make architecture` when moving imports.
- Add meaningful tests for behavior changes, particularly workflow state and process cleanup.
- Use async I/O for long operations. Never send background process output to a terminal
  owned by an interactive agent.
- Add dependencies only when used. Run `uv add package` or `uv add --dev package`,
  review its license, and commit both `pyproject.toml` and `uv.lock`.
- Use `make upgrade` to refresh pinned versions, then run checks and `make audit`.
  Routine resolution excludes releases newer than 15 days; urgent security updates
  can be selected explicitly with uv's `--exclude-newer` override after review.
- Do not commit credentials, private PR content, local databases, or session transcripts.
- Update the README and relevant guides when behavior changes. Describe what is
  implemented accurately.

## Pull requests

Keep each PR focused. Explain the problem, resulting behavior, and validation. Include
screenshots for terminal layout changes where useful, and list any untested limitations.
Small fixes need no special issue or commit-message format. AI-assisted contributions
are welcome; you are responsible for understanding and checking the submitted work.

Contributions are made under the project's [MIT license](LICENSE.txt). Retain upstream
notices when incorporating third-party material.

## Releases

Versions live in `pyproject.toml`; runtime version output reads installed package metadata.
Update the version, run `make lock`, and run `make check`,
`make smoke`, and `make audit` before a release. Publishing is intentionally manual
until package ownership and a release process are established; CI never publishes.
