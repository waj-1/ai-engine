# Common development tasks. See README.md for running the engine locally.

.PHONY: help develop run local-env local-up local-init local-down test test-coverage lint format clean lock lock-check mcp-smoke

PY := $(shell [ -x venv/bin/python ] && echo venv/bin/python || echo python3)

help: ## Show the available targets
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  %-14s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

develop: ## Create venv/ (Python 3.11) and install requirements.lock
	@[ -d venv ] || python3.11 -m venv venv
	@venv/bin/pip install --upgrade pip && venv/bin/pip install -r requirements.lock

run: ## Run the engine on http://127.0.0.1:8000
	@$(PY) main.py

local-env: ## Generate deploy/local/.env (keeps an existing one)
	@$(PY) deploy/local/gen_env.py

local-up: ## Start the local database stack
	@docker compose -f deploy/local/docker-compose.yml --env-file deploy/local/.env up -d

local-init: ## Apply the schema and create the admin user (safe to re-run)
	@set -a; . deploy/local/.env; set +a; $(PY) deploy/local/init_db.py

local-down: ## Stop the local database stack (add -v by hand to delete its data)
	@docker compose -f deploy/local/docker-compose.yml --env-file deploy/local/.env down

test: ## Run the test suite
	@$(PY) -m pytest tests/ -q

test-coverage: ## Run the test suite with an HTML coverage report
	@$(PY) -m pytest tests/ -q --cov=. --cov-report=html --cov-report=term-missing

lint: ## Check for syntax errors and undefined names
	@$(PY) -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics --exclude=venv,node_modules

format: ## Format code with black
	@$(PY) -m black .

clean: ## Remove caches and coverage output
	@find . -type d -name "__pycache__" -not -path "./venv/*" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache htmlcov .coverage

lock: ## Recompile requirements.lock from requirements.txt on linux/amd64 Python 3.11
	@docker run --rm --platform linux/amd64 -v "$(CURDIR)":/w -w /w python:3.11-slim bash -c \
		'pip install -q --upgrade pip pip-tools && \
		 pip-compile --no-emit-index-url --no-strip-extras --output-file=requirements.lock requirements.txt'

lock-check: ## Check requirements.lock still satisfies requirements.txt
	@$(PY) -m pytest tests/test_requirements_lock_sync.py -q --no-cov

mcp-smoke: ## Offline smoke test of the admin MCP server (no external services)
	@$(PY) scripts/mcp_smoke.py --offline
