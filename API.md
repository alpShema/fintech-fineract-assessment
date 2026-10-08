# API — Integration Service

Base URL (Docker Compose): `http://localhost:8002`. Every route except `GET /health`
requires `X-Api-Key: integration-service-partner-key` (a single shared key for the one
known "financing partner" caller — see `DECISIONS.md` for why this wasn't built out
into OAuth2/JWT). Every response echoes `X-Correlation-Id` (caller-supplied or
generated) — see `DECISIONS.md`'s observability section for how this and
`external_reference` work together.

The public contract is **`client_uid`**, **`loan_reference`**, and
**`external_reference`** — a Fineract or Mock ERP internal numeric id never appears
as something a caller needs to supply or track.

## Endpoints

| Method | Path | Idempotent on | Notes |
|---|---|---|---|
| GET | `/health` | — | No auth |
| POST | `/api/accounts/sync` | `external_reference` (per account) | Optional `account_code` filter |
| POST | `/api/clients` | `external_reference` + unique `client_uid` |
| GET | `/api/clients/{client_uid}` | — |
| POST | `/api/loans` | `external_reference` | Creates **and** auto-approves |
| GET | `/api/loans/{loan_reference}` | — |
| POST | `/api/loans/{loan_reference}/disburse` | `external_reference` **and** `scope_key=loan_reference` | See idempotency contract below |
| POST | `/api/loans/{loan_reference}/repayments` | `external_reference` only | No scope_key — many legitimate repayments per loan |
| GET | `/api/transactions/{external_reference}` | — | Searches across every operation type |
| POST | `/api/reconciliation` | — (read-only) | Date range or explicit `external_references` |
| POST / GET / DELETE | `/api/admin/fault-injection` | — | Test/demo harness, Part J |
| POST | `/api/admin/recover` | — | Recovery sweep, Part J |

## Examples

All examples below are real captured output (from `scripts/demo-part-e-f.sh` and
`scripts/demo-part-j-failure-scenarios.sh`'s evidence logs), not invented.

### Create a client

```
POST /api/clients
{"external_reference": "REF-DEMO-CLIENT-1", "client_uid": "SME-DEMO-1", "firstname": "Demo", "lastname": "Borrower"}

201
{"client_uid": "SME-DEMO-1", "status": "Active", "display_name": "Demo Borrower"}
```

### Create + auto-approve a loan application

```
POST /api/loans
{"external_reference": "REF-DEMO-LOAN-1", "client_uid": "SME-DEMO-1", "loan_reference": "LN-DEMO-1",
 "principal": "1000000", "expected_disbursement_date": "2026-10-08"}

201
{"loan_reference": "LN-DEMO-1", "status": "Approved"}
```

### Disburse

```
POST /api/loans/LN-DEMO-1/disburse
{"external_reference": "REF-DEMO-DISB-1", "actual_disbursement_date": "2026-10-08"}

201
{"loan_reference": "LN-DEMO-1", "status": "Active", "fineract_transaction_id": 31, "erp_transaction_id": 9}
```

### Resend the same disbursement with a DIFFERENT reference, same loan

```
POST /api/loans/LN-DEMO-1/disburse
{"external_reference": "REF-DEMO-DISB-1-RETRY", "actual_disbursement_date": "2026-10-08"}

200   <-- replay, not a second disbursement
{"status": "Active", "loan_reference": "LN-DEMO-1", "fineract_transaction_id": 31, "erp_transaction_id": 9}
```

### Repay

```
POST /api/loans/LN-DEMO-1/repayments
{"external_reference": "REF-DEMO-REPAY-1", "transaction_date": "2026-10-08", "transaction_amount": "100000"}

201
{"loan_reference": "LN-DEMO-1", "status": "Accepted", "fineract_transaction_id": 33, "erp_transaction_id": 10}
```

### Reconciliation report

```
POST /api/reconciliation
{"date_from": "2026-01-01", "date_to": "2026-12-31"}

200
{
  "items": [
    {"external_reference": "REF-K-MATCHED-1", "status": "MATCHED", "fineract_amount": "500000.0", "erp_amount": "500000.00"},
    {"external_reference": "REF-K-MISMATCH-1", "status": "AMOUNT_MISMATCH", "fineract_amount": "500000.0", "erp_amount": "1234.00"},
    {"external_reference": "REF-K-MISSING-ERP-1", "status": "MISSING_IN_ERP", "fineract_amount": "500000.0", "erp_amount": null},
    {"external_reference": "REF-K-MISSING-FINERACT-1", "status": "MISSING_IN_FINERACT", "fineract_amount": null, "erp_amount": "75000.00"}
  ],
  "summary": {"MATCHED": 1, "MISSING_IN_ERP": 1, "MISSING_IN_FINERACT": 1, "AMOUNT_MISMATCH": 1, "REVERSED": 0}
}
```

## Errors

Every error is `{"error_code": str, "message": str, "correlation_id": str | None}`.

| Status | error_code | When |
|---|---|---|
| 401 | — (`detail` only, FastAPI's own shape) | Missing or wrong `X-Api-Key` |
| 404 | `NOT_FOUND` | Unknown `client_uid`/`loan_reference`/`external_reference` |
| 409 | `CONFLICT` | Same `external_reference` (or disbursement's `scope_key`) with a *different* payload — a genuine idempotency conflict |
| 422 | `VALIDATION_ERROR` | Request body fails schema validation |
| 502 | `UPSTREAM_ERROR` | Fineract or the ERP returned an error this service can't resolve inline (includes "Fineract unreachable" and "Fineract has no record of this transaction after the grace period") |

A `202 Accepted` is not an error — it means `IN_PROGRESS`, `NEEDS_RECONCILIATION`, or
mid-flight generally; see the idempotency contract below.

## The idempotency contract

Every write endpoint follows the same rule (`app/idempotency.py`, one implementation
reused everywhere — see `DECISIONS.md`):

| Same `external_reference`? | Same payload? | Prior status | Response |
|---|---|---|---|
| No (new) | — | — | Do the real work; `201` |
| Yes | No | any | `409 CONFLICT` |
| Yes | Yes | `IN_PROGRESS`/`FINERACT_DONE`/`ERP_PENDING` | `202`, `{"status": "<that status>"}` |
| Yes | Yes | `NEEDS_RECONCILIATION` | `202`, `{"status": "NEEDS_RECONCILIATION"}` — money already moved, a recovery sweep owns catching it up |
| Yes | Yes | `COMPLETED` | `200`, the **original** result body (never the original creation status, e.g. not `201` again) |
| Yes | Yes | `FAILED` | The original failure status replayed (GL account sync is the one exception — see below) |

**Disbursement's special case**: uniqueness is *also* enforced per-loan
(`scope_key = loan_reference`), independent of `external_reference` — "concurrent
requests with different references for the same loan still result in exactly one
Fineract disbursement" is a brief requirement, not optional behavior. A scope_key
match replays the same way as an external_reference match, with one exception: a
**FAILED** scope_key match (nothing ever actually succeeded at Fineract) does not
permanently block the loan — a fresh attempt with a new reference is allowed to
retry for real, since there's no completed business fact to protect (see
`DECISIONS.md`'s Part I section for the bug this fixed).

**GL account sync's special case**: a FAILED sync is retried for real on the next
sync call (not replayed forever) — the brief is explicit that a transient Fineract
outage must be retryable, not a permanent sync failure.

**Concurrency**: the above is the *API-level* behavior. The actual enforcement under
real concurrent requests is a database unique constraint (`uq_operation_external_
reference`, `uq_operation_scope_key`), not the check table above by itself — see
`DECISIONS.md`'s Part I section for the full "why one layer isn't enough" argument.
