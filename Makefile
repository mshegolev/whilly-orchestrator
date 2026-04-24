.DEFAULT_GOAL := help
SHELL := /bin/bash

PYTHON ?= python3
PIPX ?= pipx
PKG := whilly-orchestrator

.PHONY: help install install-dev uninstall lint format test version

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo ""
	@echo "Default user install:       make install        (pipx, isolated CLI, release version from PyPI)"
	@echo "Contributor install:        make install-dev    (editable link to this checkout + dev extras)"

install: ## Prod install — isolated CLI via pipx from PyPI
	@command -v $(PIPX) >/dev/null 2>&1 || { \
		echo "pipx not found. Install it first:"; \
		echo "  macOS:    brew install pipx && pipx ensurepath"; \
		echo "  Linux:    python3 -m pip install --user pipx && python3 -m pipx ensurepath"; \
		echo "Or use plain pip (no isolation): $(PYTHON) -m pip install $(PKG)"; \
		exit 1; \
	}
	$(PIPX) install --force $(PKG)
	@echo ""
	@echo "whilly installed at: $$(command -v whilly 2>/dev/null || echo '(not on PATH — run: pipx ensurepath)')"

install-dev: ## Dev install — editable link to this checkout via pipx + [dev] extras
	@command -v $(PIPX) >/dev/null 2>&1 || { \
		echo "pipx not found. Install it first (see 'make install' output for hints)."; \
		echo "Fallback without pipx: $(PYTHON) -m pip install -e '.[dev]'"; \
		exit 1; \
	}
	$(PIPX) install --force --editable '.[dev]'
	@echo ""
	@echo "whilly (editable) at: $$(command -v whilly 2>/dev/null || echo '(not on PATH — run: pipx ensurepath)')"
	@echo "Source tree: $$(pwd)  — edits in whilly/ reflect immediately on next 'whilly' invocation."

uninstall: ## Remove whilly from pipx (and user-site pip as a courtesy)
	-$(PIPX) uninstall $(PKG) 2>/dev/null
	-$(PYTHON) -m pip uninstall -y $(PKG) 2>/dev/null
	@echo "whilly removed (ignored missing installs)."

lint: ## Run ruff (same command CI runs)
	$(PYTHON) -m ruff check whilly/ tests/
	$(PYTHON) -m ruff format --check whilly/ tests/

format: ## Apply ruff formatter
	$(PYTHON) -m ruff format whilly/ tests/
	$(PYTHON) -m ruff check --fix whilly/ tests/

test: ## Run pytest
	$(PYTHON) -m pytest -q

version: ## Show source version vs installed CLI version (diagnoses install drift)
	@echo "Source (whilly/__init__.py): $$(grep -oE '[0-9]+\.[0-9]+\.[0-9]+' whilly/__init__.py | head -1)"
	@echo "Source (pyproject.toml):     $$(grep -oE '^version = \"[0-9]+\.[0-9]+\.[0-9]+\"' pyproject.toml | head -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
	@which whilly >/dev/null 2>&1 && echo "CLI on PATH ($$(which whilly)): $$(whilly --version 2>&1 | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+' | head -1)" || echo "CLI on PATH: not installed"
