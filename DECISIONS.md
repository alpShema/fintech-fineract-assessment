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
