.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install demo collect classify pipeline digest daily schedule feedback test lint typecheck format clean

help:  ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "Targets:\n"} /^[a-zA-Z_-]+:.*##/ { printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

install:  ## Sync dependencies (creates .venv via uv)
	$(UV) sync --extra dev

demo:  ## Boot the Phase 0 skeleton end-to-end (init DB, load profile, insert sample row)
	$(UV) run python -m signal_tracker.main

collect:  ## Run one collection pass over configured RSS feeds
	$(UV) run python scripts/collect.py

classify:  ## Classify the unclassified backlog via the configured LLM
	$(UV) run python scripts/classify.py

pipeline: collect classify  ## collect + classify in one shot

digest:  ## Build, render, and send the daily digest (use ARGS=--dry-run to preview)
	$(UV) run python scripts/digest.py $(ARGS)

daily:  ## Run the full daily orchestrator (collect + classify + digest)
	$(UV) run python scripts/daily.py $(ARGS)

schedule:  ## Start the APScheduler daemon (runs daily at DIGEST_SEND_HOUR)
	$(UV) run python scripts/schedule.py

feedback:  ## Mark a signal: ARGS="42 --action contacted" or ARGS="--list"
	$(UV) run python scripts/feedback.py $(ARGS)

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
