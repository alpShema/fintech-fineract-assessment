# Simulations

Every simulation here is scripted and re-runnable (`scripts/*.sh`,
`integration-service/concurrency_tests/`), with full output saved under
`evidence/`. This document reformats that real, already-captured output into the
brief's evidence template (Initial state / Request / Response / Fineract state / ERP
state / Integration state / Expected result / Actual result / Explanation) — nothing
below is invented; it's the real evidence, organized. The Postman collection
(`postman/collection.json`) covers the same endpoints for manual, ad-hoc exploration
and is supporting evidence only, per the brief.

---

## Part A — GL account `external_reference` idempotency (Java customization)

Script: `scripts/demo-part-a.sh`. Evidence: `evidence/part-a-demo.log`.

### Behavior 1 — new `external_reference` creates

1. **Initial state**: no GL account exists with `external_reference=GL-DEMO-...`.
2. **Request**: `POST /glaccounts` `{"name":"Loan Receivable","glCode":"DEMO-GL-DEMO-...","type":1,"usage":1,"manualEntriesAllowed":true,"externalReference":"GL-DEMO-..."}`
3. **Response**: `HTTP 200`, `{"resourceId":2}`
4. **Fineract state**: a new `acc_gl_account` row created, `resourceId=2`.
5. **ERP state**: n/a (Fineract-only test).
6. **Integration state**: n/a.
7. **Expected result**: a new `resourceId`, `HTTP 200/201`.
8. **Actual result**: `HTTP 200`, `{"resourceId":2}`.
9. **Explanation**: created successfully, matches expectation.

### Behavior 2 — identical resend is idempotent (no duplicate)

1. **Initial state**: the same `external_reference` already exists (`resourceId=2`).
2. **Request**: the *identical* payload resent (simulates ERP retry / at-least-once delivery).
3. **Response**: `HTTP 200`, `{"resourceId":2}` — same id as before.
4. **Fineract state**: still exactly one row for this `external_reference`.
5–6. n/a.
7. **Expected result**: `HTTP 200`, same `resourceId`, no duplicate row.
8. **Actual result**: `HTTP 200`, `resourceId=2`.
9. **Explanation**: PASS — idempotent return confirmed, no duplicate created.

### Behavior 3 — same reference, different payload → conflict

1. **Initial state**: same as above.
2. **Request**: same `external_reference`, a *different* payload (changed name/glCode).
3. **Response**: `HTTP 409`, `{"developerMessage":"A GL Account with external_reference 'GL-DEMO-...' already exists with a different payload than the one supplied", ...}`
4. **Fineract state**: unchanged — no second row, no mutation of the existing one.
5–6. n/a.
7. **Expected result**: `HTTP 409 Conflict`.
8. **Actual result**: `HTTP 409`.
9. **Explanation**: PASS — conflicting payload rejected, not silently applied.

### Behavior 4 — the endpoint is protected by Fineract's own permission model

1. **Initial state**: authenticated as `mifos` (has `CREATE_GLACCOUNT`) for comparison.
2. **Request**: the same create call, with no `Authorization` header at all.
3. **Response**: `HTTP 401`.
4–6. n/a — rejected before any application logic runs.
7. **Expected result**: `HTTP 401`.
8. **Actual result**: `HTTP 401`.
9. **Explanation**: PASS — Fineract's own auth/permission pipeline protects this path; nothing custom was needed or built to enforce this separately.

---

## Part G — Mock ERP idempotency (journal entries)

Script: `scripts/demo-part-g.sh`. Evidence: `evidence/part-g-demo.log`.

1. **Initial state**: chart of accounts seeded on startup — `1100 Bank`, `1200 Loan
   Receivable`, `4000 Interest Income` (ASSET/ASSET/INCOME, RWF), matching Fineract's
   Part B GL accounts exactly (same codes, same `external_reference`s).
2. **Request**: `POST /api/journal-entries` — a balanced entry (disbursement: DR
   Loan Receivable 5,000,000 / CR Bank 5,000,000), `external_reference=TXN-DEMO-...`.
3. **Response**: `HTTP 201`, the created entry with `id:2` and both lines.
4. **Fineract state**: n/a (ERP-only test).
5. **ERP state**: one journal entry, two lines, balanced.
6. **Integration state**: n/a.
7. **Expected result**: `201 Created`.
8. **Actual result**: `201`.
9. **Explanation**: created as expected.

Then, same reference: **identical resend** → `200` (same id, no duplicate — PASS).
**Same reference, unbalanced different payload** → `409 CONFLICT` (PASS — a
conflicting reuse of the business key is rejected). **An entry that doesn't balance
at all** → `422 VALIDATION_ERROR` (`"entry does not balance: total debit 100.00 !=
total credit 50.00"`) — rejected before it ever reaches the database (PASS).
**Final ERP state**: `1200` balance `+10,000,000.00`, `1100` balance `-10,000,000.00`
— the accounting-correct sign convention (asset accounts report `debit - credit`),
not a naive always-positive balance.

---

## Part I — Duplicates and concurrency

Script: `integration-service/concurrency_tests/test_concurrent_disbursement.py`
(`make test-concurrency` / `make simulate-duplicates`). Evidence:
`evidence/part-i-concurrency.log`.

### I1 — sequential duplicates (send the same disbursement 4 times)

1. **Initial state**: a fresh loan, `Approved`, not yet disbursed.
2. **Request**: the identical disbursement body sent 4 times in sequence.
3. **Response**: `201, 200, 200, 200` — one real creation, three replays.
4. **Fineract state**: exactly 1 disbursement transaction on the loan.
5. **ERP state**: exactly 1 journal entry for this reference.
6. **Integration state**: exactly 1 operation, `COMPLETED`.
7. **Expected result**: Requests received: 4; Fineract disbursements: 1; ERP journal
   entries: 1; Integration transactions: 1.
8. **Actual result**: all four counts matched exactly.
9. **Explanation**: PASS — checked via four *independent* counts (not just the HTTP
   status codes), since any one count alone doesn't prove the end-to-end invariant.

### I2 — ≥10 concurrent disbursements, same loan

Two batches, both real `ThreadPoolExecutor`-fired concurrent HTTP requests (not
sequential) against the live Docker stack:

1. **Initial state**: a fresh loan, `Approved`.
2. **Request**: 10 simultaneous `POST .../disburse` calls — batch (a) all with the
   *same* `external_reference`; batch (b) each with a *different* one.
3. **Response**: both batches — statuses `[201, 202, 202, 202, 202, 202, 202, 202,
   202, 202]`. Zero `500`s.
4. **Fineract state**: exactly 1 disbursement transaction — both batches.
5. **ERP state**: exactly 1 journal entry — both batches.
6. **Integration state**: exactly one `IdempotentOperation` row reached `COMPLETED`;
   every other concurrent request either saw it directly or lost the insert race
   (`RaceLost`, caught, converted to a clean replay — see `DECISIONS.md`).
7. **Expected result**: exactly one disbursement regardless of whether the 10
   concurrent callers used the same or different references.
8. **Actual result**: matched exactly, both batches.
9. **Explanation**: PASS. Enforced by the database unique constraint
   (`uq_operation_scope_key`) — the API-level pre-check is a fast path, not the
   guarantee; `start_operation()`'s INSERT happens *before* the Fineract call, so
   only the winner ever reaches Fineract. Last line of defence (confirmed
   separately, live): Fineract's own loan state machine rejects a second disburse
   command on an already-`Active` loan outright. Full "why one layer isn't enough"
   argument in `DECISIONS.md`'s Part I section.

### I3 — concurrent repayments, distinct references + one duplicate

1. **Initial state**: a fresh loan, disbursed, known `initial outstanding` read
   directly from Fineract (`2,200,321.0` in the captured run).
2. **Request**: 10 simultaneous repayments — 8 distinct references, 1 reference sent
   twice concurrently (the duplicate pair).
3. **Response**: `[201 ×9, 202 ×1]` — zero `500`s.
4. **Fineract state**: exactly 9 repayment transactions (8 distinct + the duplicate
   pair collapsed to 1), distinct `externalId`s confirmed.
5. **ERP state**: exactly 9 journal entries, one per distinct reference.
6. **Integration state**: 9 `COMPLETED` operations (the post-batch recovery sweep
   call in the script found 0 rows needing recovery once the mapping/retry fixes
   below were in place).
7. **Expected result**: each distinct reference applies exactly once; the duplicate
   collapses to one; final Fineract `totalOutstanding` equals `initial outstanding -
   sum of each repayment's own recorded amount` (Fineract's authoritative
   breakdown, not merely the amounts the test sent).
8. **Actual result**: `final outstanding: 1750321.0`, `expected: 1750321.0` — exact
   match.
9. **Explanation**: PASS. Getting a clean first-pass result here required two real
   fixes found *by this exact test*: Fineract's own optimistic-lock conflict (409,
   "please try again") wasn't being retried, and a repayment that's 100% principal
   (zero interest owed that period) produced a zero-amount journal line Mock ERP
   correctly rejected. Both are documented and fixed in `DECISIONS.md`'s Tier 2
   section — this simulation is what found them, not a unit test.

---

## Part J — Failure handling and recovery, all 5 scenarios

Script: `scripts/demo-part-j-failure-scenarios.sh` (`make simulate-failures`).
Evidence: `evidence/part-j-failure-scenarios.log`. Mechanism:
`app/fault_injection.py` (admin-armed, in-process) + `app/recovery.py`.

### Scenario 1 — ERP down after Fineract success

1. **Initial state**: loan `Approved`; no ERP entry for the chosen reference yet (`404`).
2. **Request**: disburse, with `erp_journal_entry` fault-armed (`down`, 1 trigger).
3. **Response**: `202`, `{"status":"NEEDS_RECONCILIATION", ...}`.
4. **Fineract state**: `Active` — the disbursement really happened.
5. **ERP state**: still no entry (`404`).
6. **Integration state**: `NEEDS_RECONCILIATION`; resending the same reference
   replays the same status (confirmed — does not re-disburse).
7. **Expected result**: a false success is never claimed; nothing is double-posted;
   recovery (admin sweep) retries only the ERP leg and reaches exactly one entry.
8. **Actual result**: after `POST /api/admin/recover`: loan still `Active`, ERP entry
   now exists (`200`).
9. **Explanation**: PASS. Status answers "is anything persisted?" honestly (not
   `200`, not the original failure code either); the sweep never re-calls Fineract
   (`fineract_transaction_id` was already persisted the instant Fineract confirmed).

### Scenario 2 — Fineract down

1. **Initial state**: loan `Approved`.
2. **Request**: disburse, with `fineract_disburse` fault-armed (`down`).
3. **Response**: clean `502`, `{"error_code":"UPSTREAM_ERROR", "message":"Fineract unreachable: ..."}`.
4. **Fineract state**: unchanged — still `Approved` (confirmed directly against
   Fineract, not inferred).
5–6. Nothing persisted beyond a `FAILED` operation row.
7. **Expected result**: caller gets a clear failure; nothing persisted; safe to retry.
8. **Actual result**: a fresh reference, sent once the fault is no longer armed,
   succeeds normally (`201`, loan `Active`).
9. **Explanation**: PASS — and this exact check is what found a real bug: a FAILED
   disbursement was initially *permanently* blocking every future attempt for that
   loan (the `scope_key` unique-constraint row stayed dead forever). Fixed
   (`retry_scope_operation()`); this scenario is the regression check for it.

### Scenario 3 — Fineract timeout after sending

1. **Initial state**: loan `Approved`.
2. **Request**: disburse, with `fineract_disburse` fault-armed (`timeout_after_send`
   — the real call reaches Fineract and succeeds there; the client just doesn't see
   the response).
3. **Response**: `201` — the full result, including the ERP leg, completed
   transparently within the *same* request.
4. **Fineract state**: `Active`, one real disbursement transaction.
5. **ERP state**: one journal entry.
6. **Integration state**: `COMPLETED`.
7. **Expected result**: do not blindly retry; look up by `externalId` (sent as the
   Fineract transaction's own `externalId`) to find out what really happened.
8. **Actual result**: the lookup found the real transaction and completed the flow
   — the caller never sees any indication a timeout was simulated internally.
9. **Explanation**: PASS — this is the literal mechanism the brief's hint describes,
   confirmed live rather than only unit-tested.

### Scenario 4 — ERP timeout after the entry was written

1. **Initial state**: loan `Approved`.
2. **Request**: disburse, with `erp_journal_entry` fault-armed (`timeout_after_send`
   — the ERP really writes the entry; the client doesn't see the response).
3. **Response**: `202`, `NEEDS_RECONCILIATION` (even though the entry already exists
   — the service doesn't yet know that itself).
4. **Fineract state**: `Active`.
5. **ERP state**: the entry genuinely exists already.
6. **Integration state**: recovery sweep retries the ERP leg, hits Mock ERP's own
   `external_reference` idempotency (a replay, not a fresh insert) → `COMPLETED`.
7. **Expected result**: prove exactly one journal entry exists after retry.
8. **Actual result**: listing that day's entries and counting matches for the
   reference: `1`.
9. **Explanation**: PASS — the retry never creates a duplicate because Mock ERP's
   own idempotency (Part G) is the actual backstop, not anything the Integration
   Service tracks independently.

### Scenario 5 — crash between Fineract success and the ERP call

1. **Initial state**: loan `Approved`.
2. **Request**: disburse, with `erp_journal_entry` fault-armed (`down`) — simulates
   the process dying right after the Fineract leg, before ever attempting the ERP
   leg.
3. **Response**: `202`, `NEEDS_RECONCILIATION`.
4. **Fineract state** (before restart): `Active`.
5. **ERP state** (before restart): no entry (`404`) — stuck.
6. **Integration state** (before restart): `NEEDS_RECONCILIATION`, left deliberately
   unrecovered.
7. **Expected result**: recovered on restart, with **no manual action taken** —
   `docker compose restart integration-service` was used here, not the admin
   endpoint, specifically to prove this.
8. **Actual result**: after restart, before the app even finished starting (visible
   in the container logs): the startup sweep found the row and completed it. ERP
   entry now exists (`200`).
9. **Explanation**: PASS — "recovery without a worker is acceptable" per the brief;
   the startup sweep is the entire mechanism, verified against a real container
   restart, not just a function call.

---

## Part K — Reconciliation, all four outcomes

Script: `scripts/demo-part-k-reconciliation.sh` (`make simulate-reconciliation`).
Evidence: `evidence/part-k-reconciliation.log`.

1. **Initial state**: four scenarios deliberately seeded in one run:
   - a normal disbursement (expect `MATCHED`)
   - a disbursement with the ERP leg fault-injected down and **left unrecovered**
     (expect `MISSING_IN_ERP`)
   - a journal entry posted directly to Mock ERP under a reference no Fineract
     transaction will ever carry (expect `MISSING_IN_FINERACT`)
   - a real disbursement whose auto-posted entry was suppressed (fault injection)
     and replaced with one posted directly under the same reference but a
     deliberately wrong amount (expect `AMOUNT_MISMATCH`)
2. **Request**: `POST /api/reconciliation {"date_from": "<today>", "date_to": "<today>"}`.
3. **Response**: a report listing every reference seen on either side.
4. **Fineract state**: the real transaction list per loan (`GET .../transactions`),
   including the genuine `500000` disbursement for the mismatch case.
5. **ERP state**: the real journal entries, including the deliberately-wrong
   `1234.00` entry and the manually-posted orphan entry.
6. **Integration state**: n/a (reconciliation is read-only, doesn't touch
   `IdempotentOperation`).
7. **Expected result**: all four statuses present, correctly classified.
8. **Actual result** (real excerpt from the captured run):
   ```json
   {"external_reference": "REF-K-MATCHED-...", "status": "MATCHED", "fineract_amount": "500000.0", "erp_amount": "500000.00"}
   {"external_reference": "REF-K-MISMATCH-...", "status": "AMOUNT_MISMATCH", "fineract_amount": "500000.0", "erp_amount": "1234.00"}
   {"external_reference": "REF-K-MISSING-ERP-...", "status": "MISSING_IN_ERP", "fineract_amount": "500000.0", "erp_amount": null}
   {"external_reference": "REF-K-MISSING-FINERACT-...", "status": "MISSING_IN_FINERACT", "fineract_amount": null, "erp_amount": "75000.00"}
   ```
   `"summary": {"MATCHED": 73, "MISSING_IN_ERP": 2, "MISSING_IN_FINERACT": 1, "AMOUNT_MISMATCH": 1, "REVERSED": 0}` — the 73 `MATCHED` entries are every other genuine transaction accumulated from this session's own live testing, not a hand-picked small fixture; the extra `MISSING_IN_ERP` is a leftover stuck row from earlier manual testing, correctly still flagged.
9. **Explanation**: PASS — all four required outcomes present and correctly keyed
   per Fineract transaction, matched to the ERP entry carrying the same
   `external_reference`, exactly as the brief specifies. Interesting bonus finding:
   re-running the Part I concurrency script afterward, its recovery sweep correctly
   *refused* to silently overwrite the deliberately-wrong `AMOUNT_MISMATCH` entry —
   Mock ERP's own idempotency rejected the retry's (correct) payload against the
   already-existing (wrong) one, leaving that operation at `NEEDS_RECONCILIATION`
   rather than quietly "fixing" a ledger entry that was tampered with out-of-band.
