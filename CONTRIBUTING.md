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

`make test`, `make check`, and `make coverage` run two tracks in sequence:
ordinary tests use pytest-xdist with CPU-based workers, then tests marked `serial`
run alone without workers. Textual UI, terminal handoff, and native-agent pipeline
tests use the serial track because their timing and shutdown checks are sensitive
to concurrent load. Database tests with isolated temporary files remain parallel.
Mark new tests `@pytest.mark.serial` when they need this isolation.

Use `make test ARGS="-k name"` to filter both tracks, `ARGS="-n 2"` to limit
parallel workers, or `ARGS="-n 0"` to run both tracks serially. Marker filters are
also supported, for example `ARGS="-m serial"`. An empty track is allowed when
filtering; a selection matching no tests still fails. Direct `uv run pytest` runs
serially by default; use `uv run pytest -n auto -m 'not serial'` for the parallel
track only. Coverage combines both tracks before generating its reports.

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
Pushing a tag named `v<version>` runs the [publishing workflow](.github/workflows/publish.yml).
The tag must exactly match the version in `pyproject.toml`, for example `v0.5.0`
for version `0.5.0` (or `v0.6.0rc1` for `0.6.0rc1`). The workflow runs the existing
checks and installation smoke tests on Linux and macOS, plus a dependency audit
on Linux. Once all checks pass, it publishes the tested Linux-built wheel and
source distribution to PyPI. Only the separate publishing job can request a PyPI
identity token; it downloads those artifacts without checking out or running project code.

### One-time setup

1. In the GitHub repository's **Settings → Environments**, create an environment
   named `pypi`. Restrict its deployment branches and tags to selected **tags**
   matching `v*`. Leave required reviewers disabled for automatic publishing on
   tag pushes; enabling them makes each release wait for approval.
2. Configure a GitHub Actions [Trusted Publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/)
   in the PyPI project's **Publishing** settings. If the project does not exist yet,
   add a [pending publisher](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
   under your [PyPI account publishing settings](https://pypi.org/manage/account/publishing/).
   Use these exact values for the upstream repository:

   | Field | Value |
   | --- | --- |
   | PyPI project name (pending publisher only) | `diffbutler` |
   | GitHub owner | `heikkitoivonen` |
   | Repository | `diffbutler` |
   | Workflow filename | `publish.yml` |
   | Environment | `pypi` |

   No PyPI API token or GitHub secret is needed. You must own an existing PyPI
   project to configure it; a pending publisher does not reserve the project name.
3. Use a GitHub tag ruleset for `v*` to limit release tag creation, modification,
   and deletion to maintainers authorized to publish.

### Publish a version

Update `pyproject.toml`, run `make lock`, and run `make check`, `make smoke`, and
`make audit`. Commit the version and lockfile changes and merge them, together with
the publishing workflow, before tagging the intended release commit. For example,
if its package version is `0.5.0` and that version has not already been published:

```sh
git tag -a v0.5.0 -m "Release 0.5.0"
git push origin v0.5.0
```

Monitor **Actions → Publish to PyPI** for the result. A GitHub Release is optional;
the tag push triggers publishing. Ordinary branch pushes and pull requests only
run CI. After publication, users can install with:

```sh
uv tool install --python 3.14 diffbutler
```

If setup or checks fail before upload, fix the issue and rerun the failed workflow
jobs when the tagged source is still correct. Source changes need a new version
and tag. PyPI does not allow replacing uploaded files; after a successful or partial
upload, use a new version for corrections rather than moving a published tag.
