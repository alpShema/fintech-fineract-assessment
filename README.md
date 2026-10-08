# fintech-fineract-assessment

A fintech integration assessment: a customized Apache Fineract core banking
platform, a mock ERP accounting system, and a Python Integration Service that
bridges the two — with idempotency, concurrency safety, failure recovery, and
reconciliation built as first-class requirements rather than afterthoughts.

For the deeper documents: **[ARCHITECTURE.md](ARCHITECTURE.md)** (components, data
flow, data model, state machine), **[API.md](API.md)** (endpoints, examples, errors,
the idempotency contract), **[SIMULATIONS.md](SIMULATIONS.md)** (every scripted
simulation with before/after state), **[DECISIONS.md](DECISIONS.md)** (trade-offs,
rationale, what wasn't built, known weaknesses).

## Quick start

```bash
docker compose up -d --build
```

Brings up four containers:

| Service | Port | What it is |
|---|---|---|
| `db` | 5432 | Postgres 16 — one instance, three separate databases/roles (Fineract's tenant DBs, Mock ERP, Integration Service) |
| `fineract` | 8443 (HTTPS), 8080 | The customized Fineract core banking platform (Part A) |
| `mock-erp` | 8001 | A small FastAPI service standing in for the ERP's accounting system |
| `integration-service` | 8002 | The Python service bridging Fineract and the ERP |

First boot takes a few minutes (Fineract's own Liquibase migrations run on first
startup). `docker compose ps` should show all four containers `healthy` once ready.

`scripts/setup-fineract-environment.sh` provisions the Fineract-side environment
(office, loan product, accounting rule, least-privilege API user) — run once against
a fresh stack; see that script and `DECISIONS.md`'s Part B section for details.

## Tiers completed

Both tiers in the brief are fully built, tested, and verified against the live
running stack (not just mocked unit tests):

- **Tier 1 — Core** (Parts A–G): Fineract customization for ERP-owned GL account
  sync with `external_reference` idempotency (Java); Fineract environment setup;
  client/loan lifecycle with `client_uid`/`loan_reference` as the public identifiers;
  the Integration Service (accounts, clients, loans, transactions); the Mock ERP.
- **Tier 2 — Reliability** (Parts H–K): accounting (journal entries derived from
  Fineract's own transaction data); duplicate/concurrency safety (sequential and
  real parallel, proven against the live stack); failure injection + recovery for
  5 specific failure scenarios, modeled as an explicit state machine; reconciliation
  comparing Fineract and the ERP across all four required outcomes.

## Pinned versions

- **Fineract**: upstream tag `1.10.0`, Java 17, Gradle 8.6 (`fineract-customization/NOTES.md`)
- **Python** (both `integration-service/` and `mock-erp/`): FastAPI 0.115.6, SQLAlchemy
  2.1.4, Pydantic 2.13.5, `psycopg[binary]` 3.2.10, httpx 0.28.1 — see each service's
  `requirements.txt`/`requirements-dev.txt` for the full pinned set, including dev
  tools (pytest 8.3.4, mypy 1.14.1, ruff 0.8.4).
- **Postgres**: 16.1

## Running the tests

One documented command per language — also wrapped in the root `Makefile`:

```bash
# Java (Part A's customization) — requires the fineract-customization image built once
make test-java
# equivalent to: bash scripts/run-fineract-tests.sh

# Python — Integration Service
make test-integration-service
# equivalent to: cd integration-service && python -m pytest tests/ -q && python -m ruff check app tests && python -m mypy app

# Python — Mock ERP
make test-mock-erp
# equivalent to: cd mock-erp && python -m pytest tests/ -q && python -m ruff check app tests && python -m mypy app

# Python — concurrency (Part I, needs the full stack running: `docker compose up -d` first)
make test-concurrency
# equivalent to: cd integration-service && python concurrency_tests/test_concurrent_disbursement.py
```

Both Python services use type hints checked by mypy (`--strict`) and linted by ruff;
Java's Part A customization has its own JUnit unit + integration tests
(`fineract-customization/`, evidence in `evidence/fineract-java-tests.log` and
`evidence/fineract-integration-test.log`).

## Running the simulations

Scripted, re-runnable, print before/during/after state — see `SIMULATIONS.md` for the
full write-up of each one using the brief's evidence template:

```bash
make simulate-part-a         # Part A: idempotent GL account sync, all 4 behaviors
make simulate-duplicates     # Part I: sequential + real concurrent duplicate requests
make simulate-failures       # Part J: all 5 failure/recovery scenarios
make simulate-reconciliation # Part K: all 4 reconciliation outcomes, seeded and shown together
```

Postman collection (`postman/collection.json`) and the `evidence/` logs are supporting
evidence only, per the brief — the scripts above are the primary, reproducible source.

## Repository layout

Matches the brief's suggested layout, with one naming difference: the suggested
`fineract/configuration/` is `fineract/env/` + `fineract/postgresql-init/` here (env
files and Postgres init scripts are different enough concerns that splitting them
seemed clearer — same capability, different folder name).

```
fintech-fineract-assessment/
├── README.md ARCHITECTURE.md API.md SIMULATIONS.md DECISIONS.md
├── fineract-customization/   (patch/diff, Java tests, build notes, Dockerfile)
├── integration-service/      (app/, tests/, concurrency_tests/, Dockerfile)
├── mock-erp/                 (app/, tests/, Dockerfile)
├── fineract/                 (env/, postgresql-init/)
├── scripts/                  (setup + demo/simulation scripts)
├── docker-compose.yml
├── postman/collection.json
├── Makefile
└── evidence/
```
