.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install demo test lint typecheck format clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:  ## Sync dependencies (creates .venv via uv)
	$(UV) sync --extra dev

demo:  ## Boot the Phase 0 skeleton end-to-end (init DB, load profile, insert sample row)
	$(UV) run python -m signal_tracker.main

test:  ## Run pytest
	$(UV) run pytest

lint:  ## Run ruff
	$(UV) run ruff check .

format:  ## Auto-format with ruff
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck:  ## Run mypy --strict
	$(UV) run mypy

clean:  ## Remove caches and the local SQLite DB
	rm -rf .mypy_cache .ruff_cache .pytest_cache
	rm -f data/signals.db
