.PHONY: up down test-java test-integration-service test-mock-erp test-concurrency \
        simulate-part-a simulate-duplicates simulate-failures simulate-reconciliation

up:
	docker compose up -d --build

down:
	docker compose down

# Part A's Java tests run against a local scratch clone of the pinned Fineract tag
# (never vendored into this repo -- see fineract-customization/NOTES.md for why).
# Usage: make test-java FINERACT_CLONE=/path/to/fineract-upstream-clone
test-java:
	bash scripts/run-fineract-tests.sh "$(FINERACT_CLONE)"

test-integration-service:
	cd integration-service && python -m pytest tests/ -q && python -m ruff check app tests && python -m mypy app

test-mock-erp:
	cd mock-erp && python -m pytest tests/ -q && python -m ruff check app tests && python -m mypy app

# Part I -- needs the full stack already running (`make up`).
test-concurrency:
	cd integration-service && python concurrency_tests/test_concurrent_disbursement.py

# Simulations -- all scripted, re-runnable, print before/during/after state.
# See SIMULATIONS.md for the written-up results using the brief's evidence template.
simulate-part-a:
	bash scripts/demo-part-a.sh

simulate-duplicates: test-concurrency

simulate-failures:
	bash scripts/demo-part-j-failure-scenarios.sh

simulate-reconciliation:
	bash scripts/demo-part-k-reconciliation.sh
