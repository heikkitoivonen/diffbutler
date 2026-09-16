# Dependency licensing

DiffButler's own source is licensed under MIT. The terminal handoff experiment uses
Textual (MIT); development tools are not vendored into its wheel.
There is no dependency in this scaffold that requires changing the project license.

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

The audit tool transitively installs certifi, whose certificate bundle is MPL-2.0.
It is a development dependency and is not bundled in DiffButler. Its file-level
requirements do not require relicensing DiffButler's separate source files; preserve
its terms if redistributing it. See [certifi's license](https://github.com/certifi/python-certifi/blob/master/LICENSE)
and [Mozilla's MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

When adding a dependency, review its actual license and bundled notices before merging.
Prefer permissive dependencies; discuss copyleft or noncommercial restrictions first.
Dependencies retain their own licenses. Distributing a standalone bundle containing
them requires preserving applicable license/notice files, not relabeling them MIT.
Recheck the dependency inventory before distributing such a bundle.

The larger dependency list in SPEC.md is a plan, not the installed dependency set.
