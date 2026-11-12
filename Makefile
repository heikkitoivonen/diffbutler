.DEFAULT_GOAL := help
UV ?= uv
RUN := $(UV) run --locked

.PHONY: help setup run handoff format lint typecheck architecture test coverage check build smoke site diagrams audit licenses lock upgrade

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "DiffButler development commands\n\n"} /^[a-zA-Z_-]+:.*## / {printf "  %-16s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install Python 3.14 and the locked development environment
	$(UV) python install 3.14
	$(UV) sync --locked

run: ## Run the CLI (pass ARGS="--version" or ARGS="--help")
	$(RUN) diffbutler $(ARGS)

handoff: ## Run the offline terminal handoff prototype (c attach, q quit)
	$(RUN) python -m diffbutler.ui.tui.handoff_probe $(ARGS)

format: ## Apply safe Ruff fixes and format Python code
	$(RUN) ruff check --fix .
	$(RUN) ruff format .

lint: ## Check lint, formatting, and GitHub Actions security without changing files
	$(RUN) ruff check .
	$(RUN) ruff format --check .
	$(RUN) zizmor --offline --quiet .github

typecheck: ## Run strict Pyright checks
	$(RUN) pyright

architecture: ## Verify import boundaries
	$(RUN) lint-imports

test: ## Run parallel tests, then serial integrations (ARGS="-k name" to filter)
	$(RUN) python scripts/run_tests.py $(ARGS)

coverage: ## Run tests with branch coverage and HTML/XML reports
	$(RUN) python scripts/run_tests.py --coverage $(ARGS)

check: lint typecheck architecture test ## Run all local quality checks

build: ## Build the source distribution and wheel
	$(UV) build

smoke: build ## Install the wheel in a fresh environment and check both entry points
	$(RUN) python scripts/check_dist.py

site: ## Build the README website in .cache/site
	$(RUN) python scripts/build_site.py

MMDC ?= npx --yes @mermaid-js/mermaid-cli@12.0.0

diagrams: ## Render docs/diagrams/*.mmd to SVG (requires Node; pass MMDC_ARGS for mmdc options)
	@for source in docs/diagrams/*.mmd; do \
		$(MMDC) --quiet -c docs/diagrams/mermaid.json -b white $(MMDC_ARGS) -i "$$source" -o "$${source%.mmd}.svg" || exit 1; \
	done

audit: ## Audit locked runtime and development dependencies (requires network)
	@mkdir -p .cache
	$(UV) export --locked --all-groups --no-emit-project --format requirements-txt --output-file .cache/audit-requirements.txt --quiet
	$(RUN) pip-audit --disable-pip --require-hashes --requirement .cache/audit-requirements.txt

licenses: ## List installed dependency license metadata
	$(RUN) python scripts/licenses.py

lock: ## Refresh the lockfile after editing dependency declarations
	$(UV) lock

upgrade: ## Upgrade locked dependencies within declared constraints
	$(UV) lock --upgrade
	$(UV) sync --locked
