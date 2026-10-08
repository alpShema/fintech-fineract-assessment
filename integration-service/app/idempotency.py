"""Part F: the external_reference idempotency contract, implemented once and
reused by every endpoint that needs it, rather than each router reinventing
its own version of "new / replay / in-progress / conflict."

    new reference                          -> process once
    same reference, same payload, finished -> return the original result (200)
    same reference, same payload, running  -> 202 with current status
    same reference, different payload      -> 409

Payloads are compared via a JSON round-trip (Pydantic's `.model_dump(mode="json")`)
on both sides -- not raw Python object equality -- so a freshly-parsed request and
a previously-stored-then-reloaded one compare the same way regardless of which
Python types a particular field happened to be constructed with.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.idempotent_operation import IdempotentOperation, OperationStatus, OperationType


class IdempotencyConflict(Exception):
    """Same external_reference (or, for scoped operations, the same scope_key)
    already exists with a different payload. Maps to HTTP 409."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


@dataclass
class IdempotencyDecision:
    is_new: bool
    """True: no prior record, caller should do the real work and call
    record_result() when done. False: a prior record exists and `replay_*`
    below is already the answer -- caller must not repeat the real work."""

    operation: IdempotentOperation | None = None
    replay_status_code: int | None = None
    replay_body: dict[str, Any] | None = None


def check_idempotency(
    db: Session,
    operation_type: OperationType,
    external_reference: str,
    payload: dict[str, Any],
    scope_key: str | None = None,
) -> IdempotencyDecision:
    """Call this first, before doing any real work. Raises IdempotencyConflict
    (409) if this is a genuine conflict. Returns a decision telling the caller
    whether to proceed (is_new=True) or replay a cached result (is_new=False)."""

    existing = db.execute(
        select(IdempotentOperation).where(
            IdempotentOperation.operation_type == operation_type,
            IdempotentOperation.external_reference == external_reference,
        )
    ).scalar_one_or_none()

    if existing is not None:
        if existing.request_payload != payload:
            raise IdempotencyConflict(
                f"{operation_type.value} with external_reference '{external_reference}' already exists "
                "with a different payload"
            )
        if existing.status == OperationStatus.IN_PROGRESS:
            return IdempotencyDecision(
                is_new=False,
                operation=existing,
                replay_status_code=202,
                replay_body={"status": "IN_PROGRESS", "external_reference": external_reference},
            )
        if existing.status == OperationStatus.COMPLETED:
            # Part F is explicit: "same reference, same payload, finished ->
            # Return the original result (200, same body)" -- 200, not
            # whatever status the original creation used (e.g. 201). Only the
            # status code changes on replay; the body is the real original
            # result either way.
            return IdempotencyDecision(
                is_new=False, operation=existing, replay_status_code=200, replay_body=existing.response_body
            )
        # FAILED: replay the real failure status -- claiming 200 here would
        # tell the caller a failed operation succeeded.
        return IdempotencyDecision(
            is_new=False, operation=existing, replay_status_code=existing.response_status_code, replay_body=existing.response_body
        )

    # Scope-level check (disbursement only): a *different* external_reference
    # for the *same loan* that's already disbursed. Per the brief, this must
    # still result in exactly one Fineract disbursement -- treated as an
    # idempotent-style success (the business outcome "this loan is disbursed"
    # already holds), not a 409: a 409 here would read as "you're in conflict",
    # which could make a retrying caller think something is wrong and needs
    # fixing, when the actually-correct action is simply none at all. See
    # DECISIONS.md for the full reasoning.
    if scope_key is not None:
        scoped_existing = db.execute(
            select(IdempotentOperation).where(
                IdempotentOperation.operation_type == operation_type,
                IdempotentOperation.scope_key == scope_key,
            )
        ).scalar_one_or_none()
        if scoped_existing is not None:
            if scoped_existing.status == OperationStatus.IN_PROGRESS:
                return IdempotencyDecision(
                    is_new=False,
                    operation=scoped_existing,
                    replay_status_code=202,
                    replay_body={"status": "IN_PROGRESS", "scope_key": scope_key},
                )
            if scoped_existing.status == OperationStatus.COMPLETED:
                return IdempotencyDecision(
                    is_new=False, operation=scoped_existing, replay_status_code=200, replay_body=scoped_existing.response_body
                )
            return IdempotencyDecision(
                is_new=False,
                operation=scoped_existing,
                replay_status_code=scoped_existing.response_status_code,
                replay_body=scoped_existing.response_body,
            )

    return IdempotencyDecision(is_new=True)


def start_operation(
    db: Session,
    operation_type: OperationType,
    external_reference: str,
    payload: dict[str, Any],
    scope_key: str | None = None,
) -> IdempotentOperation:
    """Persist the IN_PROGRESS row before doing the real work -- so a crash
    mid-operation leaves a detectable trace (Part J territory, but the row
    needs to exist from the start for that to be possible later).

    Race condition this guards against: two concurrent requests for the same
    external_reference (or, for disbursement, the same loan) can both pass
    check_idempotency()'s "is_new" check before either commits its row -- the
    DB's UNIQUE constraints (uq_operation_external_reference,
    uq_operation_scope_key) are the actual guarantee, not the check above.
    Raises IdempotencyConflict if the row that won the race has a different
    payload; otherwise the caller should treat this exactly like a replay
    (the race loser did no real work of its own -- see callers in routers/).
    """
    operation = IdempotentOperation(
        operation_type=operation_type,
        external_reference=external_reference,
        scope_key=scope_key,
        request_payload=payload,
        status=OperationStatus.IN_PROGRESS,
    )
    db.add(operation)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        decision = check_idempotency(db, operation_type, external_reference, payload, scope_key)
        if decision.is_new:
            # Extremely unlikely (the row that caused our IntegrityError should
            # be visible to this re-check), but fail loudly rather than loop.
            raise RuntimeError("Idempotency race: lost a row that should now exist") from exc
        raise _RaceLost(decision) from exc
    db.refresh(operation)
    return operation


class _RaceLost(Exception):
    """Internal signal: start_operation lost the insert race. Carries the
    IdempotencyDecision the caller should act on instead (same as if
    check_idempotency had returned is_new=False in the first place)."""

    def __init__(self, decision: IdempotencyDecision) -> None:
        self.decision = decision
        super().__init__("idempotency race lost")


def complete_operation(db: Session, operation: IdempotentOperation, status_code: int, body: dict[str, Any]) -> None:
    operation.status = OperationStatus.COMPLETED
    operation.response_status_code = status_code
    operation.response_body = body
    db.commit()


def fail_operation(db: Session, operation: IdempotentOperation, status_code: int, body: dict[str, Any]) -> None:
    operation.status = OperationStatus.FAILED
    operation.response_status_code = status_code
    operation.response_body = body
    db.commit()


def retry_operation(db: Session, operation: IdempotentOperation) -> None:
    """Re-arm a FAILED operation for another real attempt against the
    downstream system -- same external_reference, same payload, the caller is
    retrying rather than creating something new. An UPDATE on the existing
    row, not a new INSERT, so it doesn't touch the unique constraints that
    enforce one row per external_reference/scope_key.

    Used by GL account sync only (Part A/G's brief is explicit: "sync status
    ... SYNCED (or FAILED), with retry if Fineract is down" -- a FAILED sync
    must be retryable, not permanently stuck replaying its first failure).
    The other operation types deliberately keep check_idempotency()'s default
    "replay the real failure" behavior for a FAILED record: retrying a loan
    operation Fineract rejected for a business-rule reason (not an outage)
    isn't obviously correct to do silently on every resend, so that stays
    opt-in per endpoint rather than a blanket default.
    """
    operation.status = OperationStatus.IN_PROGRESS
    db.commit()
