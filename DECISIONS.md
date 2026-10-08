# Decisions

This document records the trade-offs and rationale behind design choices made in this
assessment, per-part, as they're built. Assumptions made where the brief was ambiguous
are also recorded here, with the reasoning for proceeding rather than blocking on them.

## Part A — ERP-owned GL accounts (Fineract Java customization)

### Customization mechanism: extend the existing GL account command handler

Three options were available: a brand-new endpoint, extending Fineract's existing
`GLACCOUNT`/`CREATE` command handler, or a separate Fineract-side component (e.g. an
event listener).

**Chosen: extend `GLAccountWritePlatformServiceJpaRepositoryImpl.createGLAccount()`**,
the method Fineract's own `CreateGLAccountCommandHandler` (`@CommandType(entity =
"GLACCOUNT", action = "CREATE")`) already delegates to.

Why: Fineract's command pipeline resolves and checks the `CREATE_GLACCOUNT` permission
*before* any handler's `processCommand()` runs. Extending the existing handler means
requirement 4 ("the endpoint is protected by Fineract's own permission model") is
satisfied by construction — there is no new permission to define, wire up, or
accidentally leave unprotected. A brand-new endpoint would have needed its own
`@CommandType`/permission registration, duplicating infrastructure Fineract already
provides and introducing a second place that permission logic could drift out of sync
with the rest of the GL account API. A separate Fineract-side component (e.g. reacting
to a `GLAccount` creation event after the fact) was rejected because the idempotent
check has to run *before* a row is written, not after — an event-driven approach would
either still need to duplicate the check inline (no benefit) or risk a window where a
duplicate briefly exists before being cleaned up.

### Conflict response: a genuine HTTP 409, not Fineract's default 403

Fineract's existing `GLAccountDuplicateException` (for `gl_code` collisions) extends
`AbstractPlatformDomainRuleException`, which `PlatformDomainRuleExceptionMapper` maps to
**403 Forbidden** — Fineract's established convention for "a valid request violated a
domain rule." The brief explicitly allows this ("409 or Fineract's equivalent").

**Chosen: a dedicated `GLAccountExternalReferenceConflictException` extending
`AbstractPlatformException` directly**, with its own `@Provider` `ExceptionMapper`
returning a literal `Response.Status.CONFLICT` (409).

Why: 403 means "you are not allowed to do this." That's the wrong signal here — the
caller *is* allowed to sync GL accounts; the problem is that the specific
`external_reference` they reused now points at conflicting data. 409 is the semantically
correct status for "this request conflicts with the current state of the resource," and
distinguishing it from Fineract's 403-domain-rule convention makes the two failure modes
(forbidden vs. idempotency-key conflict) distinguishable by HTTP status alone to any
API consumer, without needing to parse the response body. The cost was small: one new
exception class and one new mapper (~100 lines total), reusing Fineract's existing
`ApiGlobalErrorResponse` body format so the response shape is otherwise identical to
every other Fineract error.

### Database constraint as the actual source of truth

`external_reference` uniqueness is enforced at three layers, deliberately:

1. **Liquibase migration** (`db/custom-changelog/0001_...xml`) adds a real
   `UNIQUE` constraint (`uq_acc_gl_account_external_reference`) on the column — this is
   what actually prevents two rows with the same reference from ever existing, under any
   circumstance, including concurrent requests.
2. **JPA-level `@UniqueConstraint`** on `GLAccount`'s `@Table` annotation mirrors the
   Liquibase constraint (the same pattern Fineract already uses for `gl_code`) — this
   lets Hibernate's own schema generation enforce the same constraint in test profiles
   that don't run Liquibase (e.g. a future `@DataJpaTest`), and documents the constraint
   directly on the entity.
3. **Application-level pre-check** (`findOneByExternalReference` before insert) is only
   an optimization/fast-path for the common case — it answers "does this probably
   already exist" cheaply, with a friendly response, *before* paying for a failed
   INSERT.

The reason all three exist rather than just the application check: two concurrent
requests for the same new `external_reference` can both pass the pre-check (neither has
committed yet when the other reads), so relying on the application check alone would let
a race condition create a duplicate. The Liquibase-created DB constraint is the actual
last line of defense — `GLAccountWritePlatformServiceJpaRepositoryImpl
.handleGLAccountDataIntegrityIssues()` catches the resulting
`DataIntegrityViolationException` by constraint name and converts it to the same 409 a
non-concurrent conflict would produce, so the caller sees consistent behavior regardless
of whether the conflict was detected by the pre-check or by the database.

### Idempotency comparison: explicit field-by-field, not reusing `update()`

`GLAccount.update(JsonCommand)` already computes a changes-map by comparing incoming
JSON against current field values — tempting to reuse for an idempotency check. This was
deliberately **not** done: `update()` mutates the entity's fields as a side effect of
computing the diff, and since `GLAccount` is a JPA-managed entity once loaded from the
repository, those mutations can be flushed to the database by Hibernate's dirty-checking
at the end of the transaction *even if `save()` is never called*. A "read-only" equality
check built on top of `update()` could silently persist a partial change. Instead,
`isSamePayload()` only reads: it extracts every field from the incoming command into
local variables and compares them against the existing entity with `Objects.equals`,
never touching the entity's own fields.

### Create-only scope: `external_reference` is write-once

Per the brief, sync is explicitly create-only ("Updates and deletions are out of
scope"). `GLAccount.update()` was deliberately left untouched — it has no awareness of
`external_reference` at all, so Fineract's native `PUT` (update) command on a GL account
cannot change a reference once set. This wasn't an oversight; it keeps the one external
mutation path (create) as the only place this invariant needs to be reasoned about.

### Bug found and fixed: blank `external_reference` must become `NULL`, not `""`

Found while building Part B (creating Fineract-only GL accounts, like the loan
write-off/overpayment mapping accounts, that intentionally have no ERP
`external_reference`): Fineract's `JsonCommand.stringValueOfParameterNamed(...)`
returns `""` (empty string), not `null`, when the JSON field is absent entirely. Our
original `GLAccount.fromJson(...)` set whatever came back unconditionally, so **every**
GL account created without an `external_reference` ended up with `external_reference =
''` in the database — and since an empty string is a real, DB-comparable value (unlike
`NULL`, which `UNIQUE` constraints exempt from collision), creating a *second* such
account violated `uq_acc_gl_account_external_reference` against the first one. This
silently limited the entire database to one externally-unreferenced GL account ever
existing — a real correctness bug, not a hypothetical one (reproduced creating the
Part B write-off and overpayment GL accounts: the second one failed with our own 409).

Fixed by normalizing blank to `null` before setting the entity field:
```java
final String externalReferenceRaw = command.stringValueOfParameterNamed(EXTERNAL_REFERENCE.getValue());
final String externalReference = StringUtils.isBlank(externalReferenceRaw) ? null : externalReferenceRaw;
```
This is exactly the kind of gap unit tests with mocked `JsonCommand` stubs can't catch
on their own (our mocks return `null` directly, by our own choice, when a test wants
"absent") — it only surfaced testing against the **real** Fineract JSON parsing layer,
via the Part B integration work. Re-confirms why the integration test (and the Docker
rebuild + real `docker compose up` demo) are required evidence, not just nice-to-haves.

### Known weaknesses / what wasn't built

- **No distinction between "identical payload" and "same payload but caller forgot
  `description`."** `isSamePayload()` compares every field Fineract's `GLAccount.fromJson`
  reads, including optional ones like `description` and `tagId`. A resync that happens to
  omit an optional field it previously sent would be treated as a conflict (409), not a
  partial update — acceptable under "create-only," but worth knowing if the Python
  Integration Service's payload construction isn't perfectly deterministic.
- **Error code `9998`** (`FineractExceptionMapper.errorCode()`) was picked only by
  checking it doesn't collide with the two existing usages of `9999`; it isn't otherwise
  meaningful and nothing currently reads it besides the Fineract batch-API error
  enumeration mechanism.
- Sync-vs-async, retries, and partial-failure handling for the **Python side** of GL
  account sync (Part A's other half) aren't decided yet — those entries get added here
  once the Integration Service is built.

## Part B — Fineract environment configuration

### Accounting rule: cash-based

`accountingRule=2` (CASH_BASED) on the loan product. Journal entries post when cash
actually moves (disbursement, repayment), not when interest accrues. Chosen over
accrual because: (a) it's simpler to reason about and demonstrate end-to-end in the
time available, (b) it matches the brief's own suggested mapping in Part H exactly
("Disbursement: DR Loan Receivable / CR Bank" — a cash-movement-triggered entry, not an
accrual schedule), and (c) accrual accounting would require also deciding and building
a periodic accrual job, which is out of scope for what this assessment is actually
evaluating (GL sync correctness and idempotency, not accounting-period mechanics).

### Why the loan product needs 5 GL accounts, not 3

Part A only requires 3 ERP-synced accounts (Bank, Loan Receivable, Interest Income —
matching Part G's suggested chart of accounts: 1100/1200/4000). But Fineract's loan
product creation API *validates* that `writeOffAccountId` (expense) and
`overpaymentLiabilityAccountId` (liability) are present and of the correct account
*type*, even though fees/penalties/write-off/overpayment are explicitly out of scope
per the brief. Two more GL accounts were added purely to satisfy this structural
validation: "Loan Losses Written Off" (expense, glCode 5000) and "Loan Overpayments"
(liability, glCode 2100). **These two are deliberately not ERP-synced** (no
`external_reference`) — they're Fineract-only bookkeeping placeholders that exist to
make the product creation API happy, not part of the ERP's chart of accounts. Discovered
by attempting creation and reading Fineract's own validation errors rather than
guessing the full required field set upfront.

### Least-privilege API user: exactly 10 permissions, nothing else

Role "Integration Service" (id 3) is granted precisely: `CREATE_GLACCOUNT`,
`READ_GLACCOUNT` (Part A sync), `CREATE_CLIENT`, `READ_CLIENT`, `ACTIVATE_CLIENT`
(Part C), `CREATE_LOAN`, `READ_LOAN`, `APPROVE_LOAN`, `DISBURSE_LOAN`, `REPAYMENT_LOAN`
(Part D). No user/role management, no organisation-wide configuration, no permissions
for any other product type. Verified by proving both directions: a granted action
(`GET /clients` → 200) succeeds, and an ungranted action (`POST /users` → 403, "User has
no authority to: CREATE_USER") is rejected — not just that *something* works, but that
the boundary is where it's supposed to be.

**Bug found and fixed while setting this up**: `PUT /roles/{id}/permissions` rejects a
payload grouped by category (`{"accounting": {...}, "portfolio": {...}}` — which is how
`GET /roles/{id}/permissions` *displays* them) with "parameter accounting is not
supported." It wants a single flat `{"permissions": {"CODE": true, ...}}` object. The
first attempt at this *looked* like it succeeded (the script piped the response to
`/dev/null` and didn't check it), silently granting **zero** permissions — caught only
by then explicitly testing that a granted action actually worked, which it didn't,
which is what led to checking `GET /roles/{id}/permissions` and finding nothing selected.
Lesson applied: the setup script's permission-grant step now checks the response for a
`resourceId` and fails loudly if it's not there, instead of discarding it.

### Office and currency

"Kigali Main Office" created as a child of the default "Head Office" (not a
replacement for it — Fineract always seeds Head Office, and there's no reason to
delete it). RWF enabled via `PUT /currencies` alongside the default USD (not replacing
USD, since Fineract requires at least one currency and removing USD isn't necessary for
this scenario to work).

### Loan product parameters

18% per year (`interestRateFrequencyType=3`), declining-balance interest
(`interestType=0` — more realistic for an asset-finance product than flat interest),
equal installments (`amortizationType=1`), 12 monthly repayments
(`repaymentFrequencyType=2`), single disbursement (`multiDisburseLoan=false`, per the
brief's explicit "no multi-tranche"), principal range 500,000–20,000,000 RWF with a
default of 5,000,000 (matching the brief's own suggested Part D loan amount), standard
`mifos-standard-strategy` transaction processing order (penalties/fees/interest/
principal — not actually exercised since fees/penalties are out of scope, but it's
Fineract's own default and changing it wasn't warranted).

## Part C — Identities, Part D — Loan lifecycle

Demonstrated directly against Fineract (not yet through the Integration Service, which
doesn't exist as code yet) to prove the underlying mechanism before building a service
on top of it. `scripts/demo-part-c-d.sh` is the reproducible version; `evidence/part-c-d-demo.log`
is a captured run.

### `client_uid` / `loan_reference` → Fineract's `externalId`, not a custom field

No custom field needed on either side: Fineract's `Client` and `Loan` entities already
have a native `externalId`, and — found while exploring the source rather than
guessing — both already expose dedicated lookup-by-external-id endpoints out of the
box: `GET /clients/external-id/{externalId}` and `GET /loans/external-id/{externalId}`.
No Fineract internal numeric ID ever needs to appear in a request or response a caller
constructs around `client_uid`/`loan_reference` — confirmed concretely: the loan
lookup response includes `clientExternalId` (the client's own business ID) directly,
not the client's internal numeric ID, so the two business identifiers chain together
without Fineract's internal IDs ever surfacing.

### Client legal form: Person, not Entity

The business scenario is SME asset financing, so the "real" client is arguably a
business entity, not an individual. Fineract's `legalFormId=2` (Entity) requires
additional fields (constitution, main business line) that add complexity without
changing anything about what Part C/D are actually testing (the externalId mechanism
and the loan lifecycle state machine). Used `legalFormId=1` (Person) instead, modeling
the SME as its owner/representative — a common simplification in microfinance systems
modeling small businesses as individual borrowers. Documented here as a deliberate
scope choice, not an oversight.

### Loan lifecycle: reached Closed (obligations met) exactly as required

5,000,000 RWF, 18% p.a., 12 monthly installments (~458,400/month) → 3 partial
repayments of one installment each → one final lump-sum repayment for whatever the
*actual remaining balance* is at that point (queried live via `summary.totalOutstanding`
immediately before the final transaction, not a fixed assumed number) → `Closed
(obligations met)`, `principalOutstanding: 0`, `totalOutstanding: 0`. Fetching the live
balance right before the final payment (rather than hardcoding "pay off whatever's
left") matters because the exact remaining balance depends on the real amortization
schedule's principal/interest split per period, not a simple division of the total.

## Part G — Mock ERP (Python)

### Stack: FastAPI + SQLAlchemy 2.0 (typed) + Pydantic v2 + Postgres, no migration tool

Matches the Python-side default established before building started. One deliberate
deviation from a typical production setup: **no Alembic** (or other migration tool).
The schema is small, stable, and this is a mock service for an assessment, not a
system undergoing repeated schema evolution — `Base.metadata.create_all()` (idempotent,
skips existing tables) at startup keeps setup scripted/repeatable without the added
machinery a migration tool brings for marginal benefit here. Fineract's Liquibase
requirement is different in kind: that's a mandated requirement for *Fineract's own*
schema changes (Part A), not a blanket requirement for every Python component's
database.

### Chart of accounts seeded with the same codes/references already in Fineract

Seeded accounts (1100 Bank, 1200 Loan Receivable, 4000 Interest Income) use the exact
same `code` and `external_reference` values (`GL-1100`/`GL-1200`/`GL-4000`) as the GL
accounts already created in Fineract during Part B's setup. This isn't a coincidence to
maintain manually — it's the point: when the Integration Service's GL sync (Part A's
Python side) runs against this same chart of accounts, it will find Fineract's accounts
already there via the idempotent-return path (behavior 2 from Part A), not create
duplicates. One chart of accounts the whole system agrees on, not two that happen to
look similar.

### Idempotency: same pattern as Fineract's Part A, deliberately

Both `POST /api/accounts` and `POST /api/journal-entries` follow the identical
decision structure built in Java for GL accounts: new reference → create; same
reference + identical payload → return existing (200); same reference + different
payload → 409. The equality check (`_same_payload`) is explicit field-by-field
comparison, not a generic diff/mutate helper — same reasoning as Fineract's
`isSamePayload()`: a read-only check should only read.

**Bug found and fixed while building this**: the DB-constraint-violation fallback
(the last line of defense for the same create-check-then-insert race Part A's
`DECISIONS.md` describes) originally parsed `exc.orig`'s message string to figure out
*which* unique constraint fired (`"accounts_code_key" in message`). That's
Postgres-specific constraint-naming convention — SQLite (used for the fast, Docker-free
unit tests) formats the same error completely differently
(`"UNIQUE constraint failed: accounts.code"`, no constraint name at all), so the
string-matching silently failed under SQLite and a real conflict fell through as an
uncaught 500 instead of the intended 409. Caught by the test suite itself (one test
failed) before this ever reached a running container. Fixed by **re-querying** for what
actually collided instead of parsing the exception message — dialect-agnostic, works
identically against SQLite and Postgres.

### Balance sign convention is accounting-correct, not a raw debit-minus-credit

`GET /api/accounts/{code}/balance` returns a balance signed according to each
account's real normal-balance side: asset/expense accounts report `debit - credit`
(they increase on the debit side); liability/equity/income accounts report
`credit - debit`. Verified concretely: after a 5,000,000 RWF disbursement entry (DR
Loan Receivable / CR Bank), Loan Receivable reports `+5,000,000` and Bank reports
`-5,000,000` — not both positive, which a naive "always debit minus credit" calculation
would have produced for Loan Receivable but gotten backwards for Bank.

### Testing: SQLite in-memory for unit tests, real Postgres for the Docker demo

Unit tests (`tests/`) run against an in-memory SQLite database (fast, no Docker
dependency, fresh schema per test via `StaticPool`). The actual running container
(`scripts/demo-part-g.sh`) exercises the real Postgres path end to end. Both passed
independently — the SQLite-vs-Postgres dialect difference is exactly what the
bug above was about, so running the unit suite alone wouldn't have been sufficient
proof that the conflict-detection logic is actually correct against the real database.

## Parts E/F — Integration Service (Python)

### One idempotency framework (`app/idempotency.py`), reused by every endpoint

Five operation types exist (`GL_ACCOUNT_SYNC`, `CLIENT_CREATE`, `LOAN_APPLICATION`,
`LOAN_DISBURSEMENT`, `LOAN_REPAYMENT`), but there is exactly one place that implements
"new reference → process; same reference + same payload + finished → replay; same
reference + same payload + running → 202; same reference + different payload → 409."
Each router calls `check_idempotency()` → does its real work only if `is_new` → calls
`complete_operation()`/`fail_operation()`. Mirrors the decision Part A made in Java
(one `createGLAccount()` implementing the pattern once rather than four routers each
reinventing their own version) for the identical reason: five near-identical
implementations is five places a subtle bug (see below) could diverge or get fixed in
only one place and not the others.

### GL account sync trigger: explicit call, not a webhook or polling loop

Three options were available: the Mock ERP pushes a webhook on account creation, the
Integration Service polls the ERP's account list on a timer, or a caller explicitly
triggers a sync (`POST /api/accounts/sync`, optionally scoped to one `account_code`).

**Chosen: explicit call.** The Mock ERP (Part G) has no webhook/event mechanism at all
— adding one would mean building push infrastructure into a service whose own brief
never asked for it, purely to serve the Integration Service's convenience. A polling
loop would need to track "what have I already seen" to avoid hammering Fineract with
the same accounts every tick, which is exactly the job `GL_ACCOUNT_SYNC`'s
`external_reference` idempotency already does — a timer would only add latency (wait
for the next tick) and moving parts (a scheduler, start/stop lifecycle) without
removing any of the correctness work the idempotency layer already has to do anyway.
An explicit call keeps the same guarantee (safe to call repeatedly, each account's
sync idempotent on its own `external_reference`) while staying synchronous and
immediately observable — the caller's response *is* the sync result, not a
"triggered, check back later." If a real deployment later needs this to run on a
schedule, that's an external cron hitting the same endpoint, not a change to this
service.

### Sync retry when Fineract is down

The brief is specific: "Sync status per account: SYNC_PENDING → SYNCED (or FAILED),
with retry if Fineract is down." **Bug found and fixed while verifying this
requirement directly against the brief** (not caught by the original test suite,
which only ever exercised a single sync attempt per scenario): the general
`check_idempotency()` framework's "same reference, same payload, FAILED → replay the
real failure" branch, which is the right default for loan operations (see the section
above on COMPLETED-replay-is-always-200 — the same logic there about not wanting a
loan accidentally retried for a permanent business-rule rejection applies to FAILED
loan operations too), meant a GL account sync that failed once stayed permanently
stuck replaying that *same cached failure* forever. Calling sync again after Fineract
came back up would never actually try again.

Fixed with a new `retry_operation()` helper, used only by `_sync_one_account()`: when
`check_idempotency()` reports an existing FAILED record for this
`external_reference`, the router re-arms that row (`UPDATE ... SET status =
IN_PROGRESS`, not a new INSERT — the unique constraint on `external_reference` is
untouched) and genuinely retries the Fineract call, rather than trusting the
generic decision's cached replay. If Fineract is back up, the retry succeeds and the
account moves to SYNCED; if it's still down (or the failure was a genuine, permanent
conflict — e.g. two different ERP accounts claiming the same `glCode` — not a
transient outage), it fails again and reports the same real error, which is correct:
retrying doesn't fix a conflict that isn't actually transient, but it does mean the
next call tries again rather than quoting history. This is deliberately scoped to
`GL_ACCOUNT_SYNC` only — the other four operation types keep the "replay the real
failure" default, since silently re-attempting a loan operation Fineract already
rejected for a business reason isn't obviously correct to do on every resend; that
stays an opt-in choice per endpoint, not a blanket behavior change.

### Payload equality via JSON round-trip, not raw object equality

Stored and incoming payloads are compared via `.model_dump(mode="json")` on both sides
rather than comparing Python objects directly. A freshly-parsed Pydantic model and one
reloaded from the database's JSON column can differ in incidental ways (e.g. `Decimal`
vs `str`, or a `date` object vs its ISO string) that have nothing to do with whether the
*request* actually changed. Comparing the JSON-shaped form on both sides makes the
conflict check about the data the caller actually sent, not about which Python type
happened to represent it at each point.

### Replay of a COMPLETED operation is always HTTP 200, never the original creation status

The brief is explicit: "same reference, same payload, finished → return the original
result (200, same body)." The *body* returned is the real original result either way,
but the *status code* on replay is always 200 — even though the original creation was
(for example) 201. **Bug found and fixed while building this**: the first version
replayed `existing.response_status_code` unconditionally, so a resent loan-application
request got back 201 twice, which reads as "created twice" to a caller even though only
one loan was ever created in Fineract. Fixed by hardcoding 200 specifically for the
`COMPLETED` branch; a `FAILED` operation still replays its *real* failure status (e.g.
403/502), since claiming 200 for a failed operation would tell the caller it succeeded
when it didn't. The fix is the same in both the external_reference branch and the
scope_key branch of `check_idempotency()`.

### Disbursement idempotency is scoped to the loan, not the external_reference

Every other operation type is unique purely on `external_reference`. Disbursement adds
a second uniqueness axis: `scope_key = loan_reference`. Reason, straight from the brief:
"concurrent requests with different references for the same loan still result in
exactly one Fineract disbursement." A pure `external_reference` check can't catch this
— two genuinely different references are, by definition, not the same reference, so a
second disbursement request with a fresh reference would sail past that check and hit
Fineract twice. The `IdempotentOperation` table carries a second unique constraint
(`uq_operation_scope_key`, nullable — only disbursement ever sets it) and
`check_idempotency()` checks both axes: first by `external_reference` (catches retries
of the literal same request), then by `scope_key` (catches a *different* request for a
loan that's already disbursed). The scope_key hit is treated as a replay (200), not a
409: Fineract's business reality ("this loan is disbursed") already holds, and a 409
here would read as "you're in conflict, something needs fixing" to a retrying caller
when the correct response is "nothing to do, it already happened." Repayment
deliberately has **no** scope_key — unlike disbursement, many legitimate repayments
hit one loan, so uniqueness stays purely per `external_reference` there.

### Auto-approve on loan application: the brief lists no separate approve endpoint

Part E's minimum endpoint list is create/get/disburse/repayments for loans — no
standalone approve step. Rather than leave a loan stuck in `Submitted and pending
approval` with no way to move it forward through this service's own API,
`POST /api/loans` calls Fineract's create *and* approve in sequence before returning,
so every loan this service creates is immediately usable for disbursement. If a real
approval workflow (a human decision point) turns out to matter later, it is a strictly
additive change: insert a separate approve endpoint and stop auto-approving in create,
without touching the idempotency framework or the other three loan endpoints.

### API key auth, not OAuth2/JWT

A single shared `X-Api-Key` header, checked in one FastAPI dependency
(`require_api_key`), guards every route. The brief calls the caller a "financing
partner" integration — one known counterparty, not a multi-tenant public API with
distinct per-user scopes or token expiry requirements. OAuth2/JWT would add a token
issuance flow, signature verification, and expiry/refresh handling that nothing in this
assessment's scope exercises or tests. If multiple partners or per-caller permission
scoping become real requirements, the shared key is a single, isolated place to replace
with a proper auth scheme — it doesn't leak into route handlers, which only ever see
"authorized or not."

**Bug found and fixed while building this**: `require_api_key` originally declared the
header as required (`Header(...)`), which made FastAPI raise its own validation error
(422) for a *missing* key before the function body — and therefore this app's own
401 — ever ran. A missing key and a wrong key are the same failure from the caller's
point of view (not authorized), so they shouldn't produce different status codes.
Fixed by declaring the header optional (`Header(default=None)`) and checking for `None`
manually inside the function, so both cases return a clean 401.

### `client_uid`/`loan_reference` uniqueness enforced before calling Fineract, not after

`POST /api/clients` checks the local `ClientMapping` table for an existing
`client_uid` *before* ever calling Fineract — not just relying on Fineract's own
`externalId` uniqueness to reject it after the fact. Calling Fineract first and only
then discovering a local conflict would leave an orphaned Fineract client with no
local mapping pointing at it. The remaining race (two concurrent requests for the same
new `client_uid`, both passing the pre-check before either commits) is caught by the
database's own unique constraint on `ClientMapping.client_uid`; the loser's Fineract
client creation already happened by that point and is logged loudly as orphaned rather
than silently swallowed — full automatic recovery from that state is Part J/K
territory (reconciliation and failure-recovery, Tier 2), not built yet.

### Account-type mapping kept in exactly one tested module

The ERP's chart of accounts uses a string `type` (`ASSET`/`LIABILITY`/`EQUITY`/
`INCOME`/`EXPENSE`); Fineract's GL accounts use an integer type code. The mapping
(`app/account_type_mapping.py`) is a single small function with its own dedicated
test file, confirmed against Fineract's actual source rather than guessed
(`ASSET=1, LIABILITY=2, EQUITY=3, INCOME=4, EXPENSE=5`) — not inlined at each call
site, since a silent off-by-one here would misclassify real accounting entries without
any error ever being raised.

### Verified against the real stack, not just mocked unit tests

The unit suite (`respx`-mocked httpx calls, SQLite in-memory DB) covers the
idempotency/conflict/replay logic in isolation and is deliberately the heaviest-weighted
part of the test suite, matching the brief's own 20% weighting for the idempotency
contract. Separately, `scripts/demo-part-e-f.sh` runs the full account-sync →
client → loan-application → disburse → repay → transaction-lookup flow against the
actual Dockerized Fineract + Mock ERP + Postgres stack through this service's own HTTP
API, confirming the same contract holds end to end, not just against mocked
dependencies. **Bug found this way, not by the unit suite**: replaying a previously
`FAILED` GL account sync (`POST /api/accounts/sync` called again after a real Fineract
rejection) returned `status: "FAILED"` but a `null` error — the per-account replay
branch in `_sync_one_account()` populated `fineract_gl_account_id` from the stored
result but never carried the stored `error` message across the replay path. Fixed, and
a regression test (`test_resync_of_previously_failed_account_still_reports_error`)
added so the unit suite would catch this specific shape of bug on its own next time.

## Tier 2 — Reliability (Parts H, I, J, K)

Everything in this section was verified against the real running stack (Fineract
1.10.0, Mock ERP, Postgres), not just the mocked unit suite — several of the bugs
below were only ever visible under real concurrency or real Fineract responses, and
would not have been caught by respx-mocked tests alone.

### Part H — Accounting mapping

`app/accounting_mapping.py` is a pure function pair, `disbursement_journal_lines()`
and `repayment_journal_lines()`, taking a Fineract transaction dict exactly as
`GET /loans/{id}?associations=transactions` returns it — confirmed live against a
real disbursement and repayment on the running container before writing the mapping,
not assumed from documentation. Disbursement: DR Loan Receivable (1200) / CR Bank
(1100), both for the transaction's `amount` (Fineract doesn't split a disbursement
into principal/interest — the whole amount is principal movement). Repayment: DR
Bank (amount) / CR Loan Receivable (`principalPortion`) / CR Interest Income
(`interestPortion`, with fees/penalties folded in). The split comes from Fineract's
own transaction breakdown — **never recalculated here** — satisfying the brief's
explicit requirement directly: this service has no interest-calculation logic of its
own to get out of sync with Fineract's.

Fees/penalties fold into Interest Income rather than raising, because a loan product
with charges is an ordinary case (this system has no dedicated fee/penalty GL
account, and erroring on every repayment of a product that happens to have fees
would be wrong). Overpayment (`overpaymentPortion > 0`) does raise — it genuinely
needs a suspense/liability account this chart of accounts doesn't have. Reversals: a
Fineract transaction with `manuallyReversed: true` is never fed to this mapping at
all — Part K's reconciliation excludes it from normal matching and reports it under
its own `REVERSED` bucket instead, since comparing it against a journal entry would
compare a stale amount against something that no longer reflects reality. Per the
brief, documenting overpayment/reversal handling is required but demonstrating it is
optional — a real system would need a suspense account and a reversing entry (DR/CR
swapped, keyed off a derived reference like `{original}-REVERSAL`); neither is built.

**Bug found live, not by the unit suite**: a repayment that's entirely principal (no
interest owed that period — a real, common case, not an edge case) produced an
Interest Income line with `credit=0`. Mock ERP's own schema correctly rejects a
zero-amount line (`must have exactly one of debit/credit > 0, not both or neither`),
so every one of these repayments failed with a 422. Found while running Part I's
concurrent-repayment test against the real stack — several of many rapid repayments
on one loan ended up with a zero interest portion once interest for that period had
already been paid by an earlier concurrent repayment. Fixed by omitting whichever
side (principal or interest) is genuinely zero, with a safety-net `ValueError` if
*both* would be omitted (nothing left to credit against the Bank debit).

### The state machine (shared backbone for Parts I and J)

`IdempotentOperation` (already built in Part F) gained three new `OperationStatus`
values — `FINERACT_DONE`, `ERP_PENDING`, `NEEDS_RECONCILIATION` — used only by
`LOAN_DISBURSEMENT`/`LOAN_REPAYMENT`'s two-leg flow. The brief's example state list is
`RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING -> COMPLETED`, plus
`FAILED`/`NEEDS_RECONCILIATION`. `RECEIVED` and `FINERACT_PENDING` collapse into the
existing `IN_PROGRESS` value deliberately — there is no real work done between "a
request arrived" and "about to call Fineract," so persisting them as two separate
rows would just be two states that always transition together. No existing value was
renamed, so none of Part F's already-passing tests were put at risk.

Transitions: `IN_PROGRESS` → Fineract call succeeds → `FINERACT_DONE` (with
`fineract_transaction_id` persisted **immediately**, before the ERP leg is ever
attempted — this one write is what makes a crash between the two legs recoverable) →
`ERP_PENDING` → ERP call succeeds → `COMPLETED`. A genuine Fineract rejection (a real
4xx, not a timeout) goes `IN_PROGRESS` → `FAILED` directly. An ERP failure/timeout
after `FINERACT_DONE` goes to `NEEDS_RECONCILIATION`, deliberately *not* `FAILED` —
Fineract's money already moved, so this isn't a clean "nothing happened" failure; a
recovery sweep owns retrying just the ERP leg from here.

Two new columns support this: `fineract_transaction_id` (the durable fact a crash or
stale-row sweep keys off) and `loan_reference`/`fineract_loan_id` (denormalized —
also derivable via `LoanMapping`, kept directly on the row so the recovery sweep
never needs a join per stuck row).

### Fixing `_RaceLost` — a real, pre-existing bug found by re-reading the code

`start_operation()` already raised an internal `RaceLost` exception when it lost an
insert race (an `IntegrityError` on the unique constraint), carrying the
`IdempotencyDecision` the caller should act on instead. **No router caught it,
anywhere** — all five call sites (`accounts.py`, `clients.py`, and three in
`loans.py`). Under real concurrency this surfaced as an unhandled exception → a 500,
not a clean replay. Found by direct code inspection before any concurrency test was
even written, confirmed by a new unit test (`test_start_operation_losing_the_...
_race_raises_race_lost`) that simulates the race deterministically (call
`start_operation()` twice with the same key), and later reconfirmed live under Part
I's real concurrent test. Fixed by wrapping every `start_operation()` call in a
try/except that converts `RaceLost` into the same replay response
`check_idempotency()` would have given directly.

### A second bug the fix surfaced: disbursement's scope_key could permanently brick a loan

`uq_operation_scope_key` allows exactly one row, ever, per loan (by design — that's
how "exactly one disbursement per loan" is enforced). But a FAILED disbursement
attempt (e.g. Fineract was genuinely unreachable) still occupies that one row
forever: every subsequent disbursement attempt for that loan, even with a brand new
`external_reference`, would hit the same dead FAILED row and replay its stale error —
permanently, with no path back to a real attempt. Found live while manually verifying
Part J scenario 2 ("Fineract down... safe to retry?") with a fresh reference — the
retry was wrongly blocked. Fixed with `retry_scope_operation()`: when
`check_idempotency()` reports a FAILED match found via the *scope_key* axis
(distinguished from an external_reference match via a new `matched_via` field on
`IdempotencyDecision`), the router repurposes that one row in place (new
`external_reference`, new payload, status back to `IN_PROGRESS`) rather than
replaying a dead failure — an UPDATE, never a second INSERT, so the unique
constraint is never re-tested. Deliberately scoped to the scope_key axis only: a
FAILED match on the *external_reference* axis (the literal same request resent)
still replays its real failure, same as every other operation type — that's a
different request being retried, not evidence the prior attempt definitely never
happened.

### A third bug: Fineract's own optimistic-lock conflict wasn't retried

Found live under Part I's concurrent-repayment test: firing several *genuinely
distinct* (non-duplicate) repayments at one loan simultaneously got some of them
rejected with Fineract's own `HTTP 409`,
`error.msg.platform.service.query.conflict` — Hibernate's optimistic-locking version
check failing because two writes raced against the same loan entity. Fineract's own
message literally says "please try again." This is Fineract's concurrency control
doing exactly what it's designed to do, not a business-rule rejection and not
something our idempotency layer is meant to prevent (these are different references,
each a legitimate, distinct repayment). Fixed in `FineractClient._request()`: a
bounded retry (5 attempts, short linear backoff) specifically on this one
globalisation code — any other 409 fails immediately, since burning retries on a
genuine business-rule conflict would be wrong.

### Fault injection (Part J: "build a way to inject failures on demand")

`app/fault_injection.py` — an in-process, admin-toggled registry (`arm`/`clear`/
`status`/`peek`/`consume`), nothing fancier (no proxy, no feature-flag service). Two
modes per target (`fineract_disburse`, `fineract_repay`, `erp_journal_entry`): `down`
(raise `httpx.ConnectError` before the real call is ever made — Scenario 2) and
`timeout_after_send` (make the real call — so the downstream genuinely processes it —
then discard the response and raise `httpx.TimeoutException` instead of returning —
Scenarios 3/4). Each armed fault auto-clears after its configured trigger count so a
forgotten toggle can't bleed into the next run. `app/routers/admin.py` exposes this
behind the same API key as every other route, plus `POST /api/admin/recover` (the
sweep, below) — clearly a test/demo harness, not a production admin surface.

**A fourth bug this surfaced**: `httpx.ConnectError` (the "down" mode) wasn't caught
anywhere in `disburse_loan`/`repay_loan` either — only `FineractError` and
`httpx.TimeoutException` were. A genuinely unreachable Fineract produced an unhandled
500, found the moment Scenario 2 was first tested live. Fixed with its own except
clause: unlike a timeout, a `ConnectError` means the request never left this process
at all — there's no ambiguity to resolve via the externalId lookup, so it fails
cleanly and immediately (`fail_operation`, 503) rather than going through the
timeout-recovery path.

### Recovery (`app/recovery.py`) — one set of functions, three callers

`recover_unconfirmed_fineract_call()` is Part J3's mechanism exactly as hinted:
"send your reference as Fineract's externalId and look it up; do not blindly retry."
Every disbursement/repayment now sends `externalId: external_reference` on the
Fineract command itself (confirmed live that Fineract accepts and echoes this back,
both on create and on a later `GET`). On a timeout, `find_loan_transaction_by_
external_id()` searches the loan's transaction list for that exact `externalId` —
found → it happened, proceed to the ERP leg using the real transaction data; not
found and still within a short grace window → leave `IN_PROGRESS` (could still be
genuinely in flight); not found past the grace window → `FAILED` (confidently: safe
to retry with a new reference).

This same function set is called from three places: inline when a Fineract call
times out (Scenario 3), inline right after a normal Fineract success to attempt the
ERP leg (`complete_fineract_leg_and_post_to_erp()`), and from
`run_recovery_sweep()` — which scans every `IN_PROGRESS`/`FINERACT_DONE`/
`ERP_PENDING`/`NEEDS_RECONCILIATION` row for the two-leg operation types and drives
each one forward one step. The sweep runs automatically once at app startup
(`main.py`'s lifespan, before the app accepts traffic) — covering Scenario 5 (crash
recovery) with zero manual intervention — and is also exposed as
`POST /api/admin/recover` for an operator (or Scenario 1's "how does the ERP catch
up"/Scenario 4's "prove one journal entry exists" demonstrations) to call on demand.
No backoff/retry-count bookkeeping was added: "recovery without a worker is
acceptable" per the brief, and each sweep invocation is itself one bounded attempt —
the calling cadence (a cron, an operator, another demo request) *is* the retry
policy, which is simple and enough for everything demonstrated here.

All five scenarios were walked through live against the real stack (not just
unit-tested) via `scripts/demo-part-j-failure-scenarios.sh`
(`evidence/part-j-failure-scenarios.log`):
1. **ERP down after Fineract success**: caller gets `202 NEEDS_RECONCILIATION`
   (never a false success); Fineract's loan state confirms the money really moved;
   resending the same reference replays the same status (never re-disburses); the
   recovery sweep retries only the ERP leg and reaches `COMPLETED`.
2. **Fineract down**: caller gets a clean `502`; the loan's Fineract state is
   confirmed unchanged (nothing persisted); a fresh reference once Fineract is back
   succeeds normally — proving the scope_key-brick fix above actually works live.
3. **Fineract timeout after sending**: the real call lands at Fineract regardless;
   the externalId lookup finds it and completes the whole flow (including the ERP
   leg) transparently within the same request — the caller never even sees a hint
   that a timeout was simulated internally.
4. **ERP timeout after the entry was written**: the entry really gets written;
   the recovery sweep's retry hits Mock ERP's own external_reference idempotency
   (not a fresh INSERT), so exactly one entry ends up existing, confirmed by listing
   today's entries and counting matches for that reference.
5. **Crash between Fineract success and the ERP call**: simulated by leaving a row
   at `NEEDS_RECONCILIATION` and literally restarting the container
   (`docker compose restart`) rather than calling the admin endpoint — the startup
   sweep found and completed it before the app finished starting, with zero manual
   action taken.

### Part I — Duplicates and concurrency

**I1** (sequential ×4): the same disbursement request sent 4 times gives exactly one
`201` and three `200` replays; Fineract shows exactly one disbursement transaction;
Mock ERP shows exactly one journal entry; the transactions endpoint shows exactly one
`COMPLETED` integration operation. All four counts checked independently — any one
alone doesn't prove the end-to-end invariant.

**I2** (≥10 concurrent disbursements, same loan, both same-reference and
different-reference batches): a standalone script,
`integration-service/concurrency_tests/test_concurrent_disbursement.py` — real
`httpx.Client`s fired via `concurrent.futures.ThreadPoolExecutor` at the actually
running Dockerized service, deliberately **not** pytest (a live-stack test
accidentally collected by a bare `pytest` run in CI is a real hazard; SQLite's
in-memory unit-test DB also can't meaningfully race against itself — there's only one
connection). Both batches: exactly one `201`, the rest `202`/replay, zero `500`s,
exactly one real Fineract disbursement, exactly one ERP entry.

The financial invariant ("exactly one disbursement") is enforced by the **integration-DB
unique constraint** (`uq_operation_scope_key`) — concretely, because
`start_operation()`'s INSERT happens *before* the Fineract HTTP call in every
handler, only the row that wins the INSERT ever reaches Fineract at all; every other
concurrent request either sees the winner's row already in `check_idempotency()`, or
loses the INSERT race and gets `RaceLost` (now caught, see above). **API idempotency**
(the `check_idempotency()` pre-check) is a fast-path optimization on top of this, not
itself a guarantee — under a true race, two requests can both pass it before either
commits, which is exactly why the DB constraint, not the pre-check, is what's load-bearing.
Two more layers exist independent of this service entirely: **Fineract's own loan
externalId** (one Fineract loan per `loan_reference` — verified in Part C/D — so
there's only ever one real loan to disburse in the first place) and, as the **true
last line of defence**, **Fineract's own loan state machine** — confirmed live by
attempting to disburse an already-`Active` loan a second time directly against
Fineract: it rejects with its own domain-rule violation
(`loan.disbursal.account.is.not.approve.not.disbursed.state`), regardless of what any
layer above it does or fails to do. No single layer alone is enough: the API
pre-check is racy by itself; the DB constraint protects *this service's* bookkeeping
but says nothing about a caller bypassing it entirely; Fineract's own state machine
is the only layer that would still hold even if every layer above it had a bug — which
is exactly the role a last line of defence needs to play.

**I3** (concurrent repayments, distinct references, one reference duplicated and
sent twice): confirmed exactly one Fineract repayment transaction per distinct
reference (the duplicate pair collapses to one); the final Fineract
`totalOutstanding` matches `initial outstanding - sum of each repayment's own
recorded amount` (not the amounts the test sent — Fineract's authoritative
breakdown), exactly, to the cent. Getting a clean first-pass result here needed the
Part H zero-amount-line fix and the optimistic-lock retry above — without either,
several genuinely distinct repayments failed outright under concurrent load, which
would have been a false negative on I3's actual requirement (each must apply exactly
once, not "each must apply exactly once provided nothing else goes wrong first-try").
A post-batch call to the recovery sweep is included in the test as routine cleanup
for any individual repayment whose *own* ERP-posting leg loses a race under load
(same `NEEDS_RECONCILIATION` state Part J already models) — this reached zero
processed rows once the two fixes above were in place, but is kept in the script as
the honest operational step a real caller would take after a noisy concurrent batch.

### Part K — Reconciliation

Replaces the Part E stub. Scope: iterate every `LoanMapping` row (every loan this
service has ever touched — not an unbounded "all loans ever," since Fineract has no
bulk transactions-by-date-range endpoint in this version), pull each loan's full
transaction list via the single `GET /loans/{id}?associations=transactions` call
(the same endpoint Part H and Part J3 already use — one more reason this endpoint is
the right one to have standardized on everywhere), filter to disbursement/repayment
transactions carrying a non-empty `externalId`, and match each against Mock ERP's
journal entry for that same reference — fetched either individually
(`external_references` given) or via the new `GET /api/journal-entries?entry_date_
from=&entry_date_to=` list endpoint added to Mock ERP for this (it previously had no
way to enumerate entries, only create and get-by-reference — needed specifically for
the `MISSING_IN_FINERACT` direction: an ERP entry whose reference no Fineract
transaction carries at all). Amounts are compared via each side's own total
transaction magnitude (`transaction["amount"]` vs. the entry's total debit, which are
equal by construction under Part H's mapping) rather than re-deriving principal/
interest on the reconciliation side — reconciliation should notice when the two
ledgers disagree, not quietly recompute one to match the other.

A `manuallyReversed` Fineract transaction is excluded from normal matching and
reported under its own `REVERSED` status, per the reversal handling described under
Part H. Read-only by default, as the brief requires — no repair action is built;
master-data (GL account) reconciliation is explicitly out of scope as a stretch item.

All four outcomes were deliberately seeded and shown together in one report via
`scripts/demo-part-k-reconciliation.sh` (`evidence/part-k-reconciliation.log`): a
normal disbursement (`MATCHED`); a disbursement with its ERP leg fault-injected down
and left unrecovered (`MISSING_IN_ERP`); a journal entry posted directly to Mock ERP
under a reference no Fineract transaction will ever carry (`MISSING_IN_FINERACT`);
and a real disbursement whose auto-posted entry was suppressed (fault injection) and
replaced with one posted directly under the same reference but a deliberately wrong
amount (`AMOUNT_MISMATCH`). The live report returned all four statuses correctly
alongside ~70 genuinely `MATCHED` entries accumulated from the rest of this session's
live testing — itself a small extra proof that the matching logic holds up against
real accumulated state, not just a hand-picked small fixture.

## Observability: "What happened to FIN-2026000001?"

This is the brief's own worked example (Section 9), answered for real rather than
illustrated: a live disbursement was sent with `external_reference: "FIN-2026000001"`
and a caller-supplied `X-Correlation-Id: trace-fin-2026000001-demo` against the
running stack. Two structured JSON log lines came back (reformatted here for
readability; the real output is single-line JSON):

```json
{"timestamp": "2026-10-08T16:31:07.157786+00:00", "level": "INFO",
 "message": "two-leg operation completed", "logger": "app.recovery",
 "correlation_id": "trace-fin-2026000001-demo", "external_reference": "FIN-2026000001",
 "loan_reference": "LN-OBS-1791477056", "fineract_transaction_id": 182,
 "erp_transaction_id": 106, "status": "COMPLETED"}

{"timestamp": "2026-10-08T16:31:07.158494+00:00", "level": "INFO",
 "message": "loan disbursed", "logger": "app.routers.loans",
 "correlation_id": "trace-fin-2026000001-demo", "external_reference": "FIN-2026000001",
 "client_uid": "SME-OBS-1791477056", "loan_reference": "LN-OBS-1791477056",
 "fineract_transaction_id": 182, "erp_transaction_id": 106, "status": "Active"}
```

And the one API call, `GET /api/transactions/FIN-2026000001`:

```json
{
  "external_reference": "FIN-2026000001",
  "operation_type": "LOAN_DISBURSEMENT",
  "status": "COMPLETED",
  "result": {
    "status": "Active",
    "loan_reference": "LN-OBS-1791477056",
    "erp_transaction_id": 106,
    "fineract_transaction_id": 182
  }
}
```

An engineer answers the question entirely from this: the operation **completed**
(not stuck, not failed); it resolved to Fineract loan `LN-OBS-1791477056`'s
transaction `182` and ERP journal entry `106` — the two numbers needed to go look at
either ledger directly without ever having to guess or cross-reference by hand; the
`client_uid` (`SME-OBS-1791477056`) identifies whose loan this was without exposing
any Fineract-internal id as part of the lookup; and `correlation_id` is what lets this
exact request be found again in infrastructure-level logs (a load balancer, an APM
trace) that know nothing about `external_reference` at all — the two ids serve
different audiences (business/support vs. infra/on-call) and this is why both exist
side by side rather than one replacing the other.

**How this got built**: `app/logging_config.py`'s `JsonFormatter` declared all seven
required fields from the start, but most log call sites weren't actually populating
them — found by checking the repo against this exact section of the brief rather than
assuming the formatter's existence meant the job was done. `correlation_id` was
attached to response headers and error bodies but never to a single log line (zero
matches searching every router); `erp_transaction_id` was never captured at all
(`app/recovery.py`'s `_post_to_erp()` discarded Mock ERP's response entirely);
`client_uid` and `fineract_transaction_id` were missing from the disbursement/
repayment log lines specifically. Mock ERP's own routers already did this correctly
(`request: Request` on every logging handler, `getattr(request.state,
"correlation_id", None)` threaded into every `extra=`) — the integration-service fix
matches that existing, already-working sibling-service convention rather than
introducing a different mechanism.

## Sync vs. async

Every request in this system is synchronous end to end: a caller's `POST .../disburse`
blocks until the full Fineract call *and* the ERP call have both resolved (or failed),
and gets the real outcome back in that one response. No message broker, task queue, or
background worker sits in the request path anywhere.

This was a deliberate scope decision, not an oversight. The one place genuine
asynchrony would help — "what if the ERP call is slow, should the caller really wait
for it?" — is also exactly the place Part J already requires a durable, inspectable
intermediate state (`FINERACT_DONE` → `ERP_PENDING`) regardless of whether the
request path is sync or async. Introducing a queue here would mean building and
operating a second piece of infrastructure (a broker, a worker process, its own
failure modes) to solve a problem the state machine and the recovery sweep already
solve without one — and the brief says as much directly: "recovery without a worker is
acceptable." The one piece of this system that *is* asynchronous relative to the
request path is the recovery sweep itself (`app/recovery.py`'s `run_recovery_sweep()`)
— a periodic/startup catch-up for whatever a synchronous request didn't finish, not a
redesign of the main flow into a queue. If this system needed to scale past a single
instance handling its own recovery, or needed sub-second disbursement responses under
real Fineract latency, a queue would become the right next step — not needed at this
scope, and documented here as a known, deliberate boundary rather than discovered as a
surprise.

## What I chose not to build

- **Overpayment and reversal GL postings** (Part H) — both are documented (where the
  split comes from, how each *would* be handled) but not implemented; overpayment
  needs a suspense/liability account this chart of accounts doesn't have, and a
  reversal needs its own reversing entry mechanism. Demonstrating either is explicitly
  optional per the brief.
- **Reconciliation repair actions** (Part K) — the endpoint is deliberately read-only;
  any future repair action the brief allows must be explicit and idempotent, and isn't
  built here.
- **Master-data (GL account) reconciliation** — explicitly a stretch item; Part K
  covers transactions only.
- **A real job queue or scheduler** for the recovery sweep — see "sync vs. async"
  above. The admin endpoint plus the startup sweep are the entire mechanism.
- **Full OAuth2/JWT** for the Integration Service's own API — a single shared API key
  is sufficient for one known "financing partner" caller; see the Part E/F section
  above for the full reasoning.
- **Backpressure/rate-limit handling for a downstream that starts returning 429s** —
  neither Fineract nor Mock ERP rate-limits in this system, so nothing exercises this
  path today. If the ERP started returning 429s, the natural fix is the same shape as
  the existing optimistic-lock retry in `FineractClient._request()` (a bounded retry
  with backoff, scoped to that specific status/condition) — extending
  `MockErpClient.create_journal_entry()` the same way, not a new mechanism.

## Known weaknesses

- The Fineract optimistic-lock retry (5 attempts, fixed linear backoff) and the
  recovery grace period (`STALE_GRACE_PERIOD`, a flat 5 minutes) are both fixed
  constants, not adaptive (no jitter, no exponential backoff, not configurable per
  environment). Fine at this scale; a real production deployment would want these
  tunable and probably jittered to avoid synchronized retry storms under real
  concurrent load.
- Part K's reconciliation iterates every `LoanMapping` row with no pagination or
  batching. Correct and fast enough at this project's scale; would need to page
  through loans at real scale rather than pulling every transaction list in one
  request.
- Fault injection (`app/fault_injection.py`) is in-process, in-memory state — it does
  not survive a restart and only applies to the single instance it's armed against.
  That's correct for this assessment's single-instance deployment and deliberately
  simple (no external dependency), but would not work unmodified against multiple
  replicas of the Integration Service.
- Nothing alerts an operator if a row gets stuck at `NEEDS_RECONCILIATION`
  indefinitely (e.g. the ERP stays down past every sweep's attempt window) — recovery
  depends on *something* calling `POST /api/admin/recover` or reading a reconciliation
  report periodically. There is no push notification, paging, or dashboard; this is
  the honest gap between "the mechanism to recover exists" and "someone is guaranteed
  to notice it's needed."
