# Dependency licensing

DiffButler's own source is licensed under MIT. The terminal handoff experiment uses
Textual (MIT); development tools are not vendored into its wheel.
There is no dependency in this scaffold that requires changing the project license.

M0 adds Pydantic 2.13.5, pydantic-settings 2.15.0, SQLAlchemy 2.0.52,
Alembic 1.19.1, and aiosqlite 0.22.1 (all MIT). Their installed license files were
reviewed when added, along with new runtime transitive dependencies: pydantic-core
2.46.5, annotated-types 0.8.0, typing-inspection 0.4.4, and Mako 1.4.1 (MIT);
python-dotenv 1.2.3 and MarkupSafe 3.0.3 (BSD-3-Clause); and greenlet 3.5.5
(MIT, with Stackless-derived platform files under PSF-2.0). Greenlet's bundled
`LICENSE.PSF` was reviewed as well. These packages retain their installed notices;
none requires relicensing DiffButler. python-dotenv is a pydantic-settings dependency;
DiffButler does not load dotenv files.

The GitHub adapter adds HTTPX 0.28.1 (BSD-3-Clause). Installed licenses were reviewed
for HTTPX, HTTPcore 1.0.9 (BSD-3-Clause), AnyIO 4.14.2 (MIT), h11 0.16.0 (MIT),
and the existing idna 3.19 dependency (BSD-3-Clause). HTTPX also makes the existing
certifi dependency a runtime dependency; its certificate bundle and MPL-2.0 notice
were reviewed. All packages remain separately installed with their own notices.

Textual 8.2.8's installed `licenses/LICENSE` was reviewed when added. Its runtime
dependencies include Rich (MIT), markdown-it-py (MIT), mdit-py-plugins (MIT),
mdurl (MIT), linkify-it-py (MIT), Pygments (BSD-2-Clause),
platformdirs (MIT), and typing-extensions (PSF-2.0). These remain separate installed
packages with their own license notices. See [Textual's license](https://github.com/Textualize/textual/blob/main/LICENSE).

The direct development/build tools use permissive licenses:

| Tool | License | Upstream |
|---|---|---|
| uv / uv_build | MIT OR Apache-2.0 | [Astral uv](https://github.com/astral-sh/uv) |
| Ruff | MIT | [Ruff license](https://github.com/astral-sh/ruff/blob/main/LICENSE) |
| Pyright and its Python wrapper | MIT | [Pyright](https://github.com/microsoft/pyright/blob/main/LICENSE.txt), [wrapper](https://github.com/RobertCraigie/pyright-python/blob/main/LICENSE) |
| pytest | MIT | [pytest license](https://github.com/pytest-dev/pytest/blob/main/LICENSE) |
| pytest-cov | MIT | [pytest-cov](https://github.com/pytest-dev/pytest-cov) |
| pytest-asyncio | Apache-2.0 | [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio) |
| import-linter | BSD-2-Clause | [import-linter license](https://github.com/seddonym/import-linter/blob/master/LICENSE) |
| pip-audit | Apache-2.0 | [pip-audit license](https://github.com/pypa/pip-audit/blob/main/LICENSE) |

`make licenses` lists metadata for all installed packages, including transitive
development dependencies. Metadata can be incomplete and is an inventory, not an
automatic compatibility verdict. Pyright's Node extra includes Node.js and its
third-party notices; these remain with that dependency and are not copied into DiffButler.

HTTPX and the audit tool transitively install certifi, whose certificate bundle is MPL-2.0.
It is installed as a separate dependency and is not bundled in DiffButler's wheel. Its file-level
requirements do not require relicensing DiffButler's separate source files; preserve
its terms if redistributing it. See [certifi's license](https://github.com/certifi/python-certifi/blob/master/LICENSE)
and [Mozilla's MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

When adding a dependency, review its actual license and bundled notices before merging.
Prefer permissive dependencies; discuss copyleft or noncommercial restrictions first.
Dependencies retain their own licenses. Distributing a standalone bundle containing
them requires preserving applicable license/notice files, not relabeling them MIT.
Recheck the dependency inventory before distributing such a bundle.

The larger dependency list in SPEC.md is a plan, not the installed dependency set.
