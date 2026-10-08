# Architecture

## Components

```
                    ┌─────────────────────┐
  financing partner │  Integration Service │  Python / FastAPI
  (caller)  ───────▶│  (port 8002)          │
                    │  - idempotency layer  │
                    │  - state machine      │
                    │  - recovery sweep     │
                    └──────┬────────┬──────┘
                           │        │
              Fineract-Platform-TenantId      external_reference-keyed
              basic auth, HTTPS                journal entries, no auth
                           │        │
                           ▼        ▼
              ┌──────────────┐  ┌──────────────┐
              │  Fineract    │  │  Mock ERP    │   Python / FastAPI
              │  (customized)│  │  (port 8001) │
              │  port 8443   │  └──────┬───────┘
              │  (Java/Spring)│         │
              └──────┬───────┘         │
                     │                 │
                     ▼                 ▼
              ┌─────────────────────────────┐
              │         Postgres 16          │
              │  3 databases, 3 roles:       │
              │  fineract_tenants/_default,  │
              │  mock_erp, integration_service│
              └─────────────────────────────┘
```

**Fineract** (`fineract-customization/`) is Apache Fineract, upstream tag `1.10.0`,
with one real customization: `GLAccountWritePlatformServiceJpaRepositoryImpl.
createGLAccount()` gained an idempotency check keyed on a new `external_reference`
column (Liquibase-migrated, unique-constrained), so the ERP can safely resend the
same GL account creation request without creating duplicates. The patch lives at
`fineract-customization/fineract.patch`, applied at image-build time against a
freshly cloned pinned tag — Fineract's own source is never vendored into this repo.

**Mock ERP** (`mock-erp/`) stands in for the external accounting system. It owns a
chart of accounts and posts balanced double-entry journal entries, each idempotent on
its own `external_reference`. It has no knowledge of Fineract, loans, or clients —
only accounts and entries.

**Integration Service** (`integration-service/`) is the only component that knows
about both Fineract and the ERP. It exposes the public contract (`client_uid`,
`loan_reference`, `external_reference` — never a Fineract or ERP internal numeric id
as part of that contract), owns the idempotency/state-machine/recovery logic, and is
the only thing that ever writes to either Fineract or the ERP on a caller's behalf.

## Data flow: the disbursement request, end to end

This is the most structurally important flow in the system — the only one that
writes to *both* Fineract and the ERP, and therefore the one the state machine and
recovery machinery exist for.

```
1. POST /api/loans/{loan_reference}/disburse  {external_reference, actual_disbursement_date}
2. check_idempotency() -- new / replay (same or different external_reference,
   same loan) / conflict? If not new, return the cached answer; nothing below runs again.
3. Persist an IdempotentOperation row (status=IN_PROGRESS) BEFORE calling Fineract.
   This row, not the check in step 2, is what a real concurrent race is actually
   decided by (see API.md's idempotency contract and DECISIONS.md's Part I section).
4. Call Fineract: POST /loans/{id}?command=disburse, with externalId=external_reference
   on the transaction itself (so a later timeout can look this up by reference).
5. Fineract confirms success -> persist FINERACT_DONE + fineract_transaction_id
   IMMEDIATELY, before step 6 -- this one write is the entire crash-recovery story.
6. Fetch the real transaction breakdown (GET .../transactions, same call reused by
   Part K's reconciliation) -> derive journal lines (app/accounting_mapping.py,
   never recalculated independently of Fineract's own numbers) -> ERP_PENDING.
7. POST the journal entry to Mock ERP, keyed by the SAME external_reference.
8. ERP confirms -> COMPLETED. Response: {loan_reference, status, fineract_transaction_id, erp_transaction_id}.
```

If step 4 times out, times out ambiguously, or the process dies between steps 5 and
7, the row is left at a known, persisted, inspectable state — see "State machine"
below and `DECISIONS.md`'s Part J section for how each of those is actually recovered.

## Data model

**Integration Service** (Postgres, `integration_service` database):
- `idempotent_operations` — one row per state-changing request across every
  operation type (`GL_ACCOUNT_SYNC`, `CLIENT_CREATE`, `LOAN_APPLICATION`,
  `LOAN_DISBURSEMENT`, `LOAN_REPAYMENT`). Unique on `(operation_type,
  external_reference)`; disbursement additionally unique on `(operation_type,
  scope_key)` where `scope_key = loan_reference` (see API.md's idempotency contract
  for why disbursement alone needs a second axis). Carries `status` (the state
  machine, below), `request_payload`/`response_body` (JSON), and — for the two-leg
  operation types only — `loan_reference`, `fineract_loan_id`,
  `fineract_transaction_id`.
- `client_mappings` — `client_uid` (unique, the public id) → `fineract_client_id`
  (Fineract's internal numeric id, never exposed).
- `loan_mappings` — `loan_reference` (unique) → `fineract_loan_id`, plus `client_uid`
  (denormalized so a loan's owning client is available without a join).

**Mock ERP** (Postgres, `mock_erp` database):
- `accounts` — chart of accounts, `code` unique, `external_reference` unique
  (matches the same GL accounts synced into Fineract via Part A/E).
- `journal_entries` / `journal_entry_lines` — one balanced entry per
  `external_reference` (unique), each with ≥2 debit/credit lines.

**Fineract**: no new tables beyond the one Liquibase migration — a new
`external_reference` column (unique-constrained) on the existing `acc_gl_account`
table. Everything else (clients, loans, loan transactions) is Fineract's own,
unmodified schema; `externalId` (already a native field on both `Client` and `Loan`,
and now also sent on disbursement/repayment transaction commands) is what carries
`client_uid`/`loan_reference`/`external_reference` into Fineract without this system
ever needing a mapping table Fineract itself doesn't already support looking up by.

## State machine

`OperationStatus` (`integration-service/app/models/idempotent_operation.py`). Every
operation type uses `IN_PROGRESS → COMPLETED`/`FAILED`; `LOAN_DISBURSEMENT`/
`LOAN_REPAYMENT` additionally use the three states below for their two-leg
(Fineract, then ERP) flow:

```
IN_PROGRESS -> FINERACT_DONE        (Fineract call succeeded; fineract_transaction_id persisted)
IN_PROGRESS -> FAILED               (Fineract rejected outright -- real 4xx, not a timeout;
                                      safe to retry with a new external_reference)
IN_PROGRESS -> IN_PROGRESS          (timeout/crash, Fineract's outcome still unknown --
                                      resolved by looking it up via externalId, not by guessing)
FINERACT_DONE -> ERP_PENDING        (about to post the journal entry)
ERP_PENDING -> COMPLETED            (ERP accepted the entry)
ERP_PENDING -> NEEDS_RECONCILIATION (ERP failed/timed out -- Fineract's money already
                                      moved, so this is NOT a clean "nothing happened" failure)
NEEDS_RECONCILIATION -> COMPLETED   (the recovery sweep's retried ERP leg succeeded)
```

The brief's example lifecycle is `RECEIVED -> FINERACT_PENDING -> FINERACT_DONE ->
ERP_PENDING -> COMPLETED`; `RECEIVED`/`FINERACT_PENDING` collapse into the single
`IN_PROGRESS` value here since no real work happens between "a request arrived" and
"about to call Fineract." Recovery (`app/recovery.py`) is one set of functions
dispatched from three places — inline on success, inline on a Fineract timeout
(look up by `externalId`), and a sweep (startup + `POST /api/admin/recover`) that
drives any stuck row forward — see `DECISIONS.md`'s Part J section for the full
scenario-by-scenario walkthrough.

## GL sync design (Part A + Part E, one coherent story)

Two halves of the same mechanism, built in two different languages because they run
in two different systems:

1. **Fineract side (Java, Part A)**: `createGLAccount()` is idempotent on
   `external_reference` — new reference creates; same reference + identical payload
   returns the existing account (200); same reference + different payload is a real
   409. This is enforced in Fineract itself (a unique DB constraint as the last line
   of defence, same pattern as everywhere else in this system), not merely by the
   caller being polite.
2. **ERP → Fineract side (Python, Part E)**: `POST /api/accounts/sync` on the
   Integration Service pulls the ERP's chart of accounts and calls Fineract's (now
   idempotent) GL account creation for each one, itself wrapped in the same
   `IdempotentOperation` framework every other operation type uses. A GL account
   that fails to sync (e.g. Fineract was down) is retried on the next sync call
   (`retry_operation()` — the one place outside Tier 2 that already does this,
   predating Part J's more general recovery machinery).

The two halves meet at the `external_reference` value itself: the Mock ERP seeds its
chart of accounts with the exact same codes and `external_reference`s already present
in Fineract from Part B's setup, so the first real sync finds matches via the
idempotent-return path rather than creating duplicates — confirmed live, not just
asserted (see `DECISIONS.md`'s Part G section).
