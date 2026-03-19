.PHONY: env install install-py install-js dev api frontend build clean help

# ── Setup ─────────────────────────────────────────────────────────────────────

## Copy .env.example → .env and install all dependencies (run this once)
env:
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "Created .env — fill in your credentials before running 'make dev'"; \
	else \
		echo ".env already exists — skipping copy"; \
	fi
	@$(MAKE) install

install: install-py install-js

install-py:
	pip install -r requirements.txt

install-js:
	npm install
	cd frontend && npm install

# ── Development ───────────────────────────────────────────────────────────────

## Run API + frontend together (hot-reload)
dev:
	cd frontend && npm run dev &
	python -m api.server

## Run only the Flask API
api:
	python -m api.server

## Run only the Vite frontend dev server
frontend:
	cd frontend && npm run dev

## Build frontend for production
build:
	cd frontend && npm run build

# ── Utilities ─────────────────────────────────────────────────────────────────

## Generate a random SECRET_KEY and print it
secret:
	python -c "import secrets; print(secrets.token_hex(32))"

## Wipe the local SQLite database (irreversible)
clean-db:
	@echo "Deleting data/trades.db …"
	rm -f data/trades.db

## Remove compiled/generated files
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
	rm -rf frontend/dist

help:
	@grep -E '^##' Makefile | sed 's/## /  /'
