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
