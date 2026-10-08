"""Part J: the recovery machinery for LOAN_DISBURSEMENT/LOAN_REPAYMENT's
two-leg (Fineract, then ERP) flow. One set of functions, called from three
places:

  1. Inline, right after a normal disbursement/repayment's Fineract call
     succeeds -- `complete_fineract_leg_and_post_to_erp()`.
  2. Inline, when the Fineract call itself times out and we don't yet know
     whether it actually landed -- `recover_unconfirmed_fineract_call()`
     (Part J scenario 3: "send your reference as Fineract's externalId and
     look it up; do not blindly retry").
  3. The admin recovery sweep / startup sweep (`app/routers/admin.py`), for
     rows left stuck by a crash or an earlier ERP outage (Part J scenarios
     1 and 5) -- `recover_operation()` dispatches to whichever of the above
     applies based on the row's current state.

Retry policy: no inline backoff/retry loop. Each sweep invocation is one
attempt; "recovery without a worker is acceptable" per the brief, so the
sweep's own calling cadence (an operator, a cron job, or just another demo
request hitting the admin endpoint) *is* the backoff policy. An IN_PROGRESS
row with no confirmed Fineract outcome is only concluded FAILED after
STALE_GRACE_PERIOD has elapsed -- before that, it's left alone, since a
request still legitimately in flight looks identical to an abandoned one.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounting_mapping import JournalLine, disbursement_journal_lines, repayment_journal_lines
from app.clients.fineract import FineractClient
from app.clients.mock_erp import MockErpClient, MockErpError
from app.fineract_dates import parse_fineract_date_array
from app.idempotency import complete_operation, fail_operation, mark_erp_pending, mark_fineract_done, mark_needs_reconciliation
from app.models.idempotent_operation import IdempotentOperation, OperationStatus, OperationType

logger = logging.getLogger(__name__)

STALE_GRACE_PERIOD = timedelta(minutes=5)

_MAPPERS = {
    OperationType.LOAN_DISBURSEMENT: disbursement_journal_lines,
    OperationType.LOAN_REPAYMENT: repayment_journal_lines,
}


@dataclass
class RecoveryResult:
    status_code: int
    body: dict[str, Any]


def _final_response_body(operation: IdempotentOperation) -> dict[str, Any]:
    if operation.operation_type == OperationType.LOAN_DISBURSEMENT:
        return {
            "loan_reference": operation.loan_reference,
            "status": "Active",
            "fineract_transaction_id": operation.fineract_transaction_id,
        }
    return {
        "loan_reference": operation.loan_reference,
        "status": "Accepted",
        "fineract_transaction_id": operation.fineract_transaction_id,
    }


def _lines_to_entry_payload(
    operation: IdempotentOperation, transaction: dict[str, Any], lines: list[JournalLine]
) -> dict[str, Any]:
    return {
        "external_reference": operation.external_reference,
        "entry_date": parse_fineract_date_array(transaction["date"]).isoformat(),
        "description": f"{operation.operation_type.value} {operation.external_reference}",
        "lines": [{"account_code": line.account_code, "debit": str(line.debit), "credit": str(line.credit)} for line in lines],
    }


def _log_fields(operation: IdempotentOperation, correlation_id: str | None) -> dict[str, Any]:
    """The brief's observability field set, drawn from whatever this
    operation already knows about itself -- used by every log line in this
    module so a sweep-triggered recovery and an inline one log identically
    (minus correlation_id, which only an inline call actually has)."""
    return {
        "correlation_id": correlation_id,
        "external_reference": operation.external_reference,
        "loan_reference": operation.loan_reference,
        "fineract_transaction_id": operation.fineract_transaction_id,
    }


def _post_to_erp(
    db: Session,
    mock_erp: MockErpClient,
    operation: IdempotentOperation,
    transaction: dict[str, Any],
    correlation_id: str | None = None,
) -> RecoveryResult:
    mapper = _MAPPERS[operation.operation_type]
    try:
        lines = mapper(transaction)
    except ValueError as exc:
        # Genuinely unmappable (e.g. overpayment) -- not something a retry
        # fixes, needs a human to decide the GL treatment.
        detail = {"error": f"cannot derive journal entry: {exc}"}
        mark_needs_reconciliation(db, operation, detail)
        logger.error(
            "cannot derive journal entry, needs reconciliation",
            extra={**_log_fields(operation, correlation_id), "status": "NEEDS_RECONCILIATION"},
        )
        return RecoveryResult(202, {"status": "NEEDS_RECONCILIATION", "external_reference": operation.external_reference})

    mark_erp_pending(db, operation)
    entry_payload = _lines_to_entry_payload(operation, transaction, lines)
    try:
        erp_result = mock_erp.create_journal_entry(entry_payload)
    except (MockErpError, httpx.TimeoutException, httpx.ConnectError) as exc:
        mark_needs_reconciliation(db, operation, {"error": str(exc)})
        logger.warning(
            "ERP leg failed, needs reconciliation",
            extra={**_log_fields(operation, correlation_id), "status": "NEEDS_RECONCILIATION"},
        )
        return RecoveryResult(202, {"status": "NEEDS_RECONCILIATION", "external_reference": operation.external_reference})

    body = _final_response_body(operation)
    body["erp_transaction_id"] = erp_result.get("id")
    complete_operation(db, operation, 201, body)
    logger.info(
        "two-leg operation completed",
        extra={**_log_fields(operation, correlation_id), "erp_transaction_id": erp_result.get("id"), "status": "COMPLETED"},
    )
    return RecoveryResult(201, body)


def complete_fineract_leg_and_post_to_erp(
    db: Session,
    mock_erp: MockErpClient,
    operation: IdempotentOperation,
    fineract_transaction: dict[str, Any],
    correlation_id: str | None = None,
) -> RecoveryResult:
    """Normal happy path: Fineract just confirmed success inline. Persist that
    fact immediately (so a crash from here on is recoverable), then attempt
    the ERP leg."""
    mark_fineract_done(db, operation, fineract_transaction["id"])
    return _post_to_erp(db, mock_erp, operation, fineract_transaction, correlation_id=correlation_id)


def recover_unconfirmed_fineract_call(
    db: Session,
    fineract: FineractClient,
    mock_erp: MockErpClient,
    operation: IdempotentOperation,
    correlation_id: str | None = None,
) -> RecoveryResult:
    """Part J scenario 3: the Fineract call itself timed out (or the process
    crashed before recording the outcome) and operation.fineract_transaction_id
    is still unknown. Look up by externalId -- the exact reference we sent as
    the transaction's own externalId -- rather than blindly retrying, which
    could double-disburse if the first attempt actually landed."""
    assert operation.fineract_loan_id is not None  # always set at operation-creation time for these two operation types
    transaction = fineract.find_loan_transaction_by_external_id(operation.fineract_loan_id, operation.external_reference)
    if transaction is not None:
        return complete_fineract_leg_and_post_to_erp(db, mock_erp, operation, transaction, correlation_id=correlation_id)

    # SQLite (unit tests) returns naive datetimes even for a `DateTime(timezone=True)`
    # column; Postgres (real deployment) returns tz-aware ones. Coerce rather
    # than let the two dialects disagree on whether this comparison is even legal.
    created_at = operation.created_at if operation.created_at.tzinfo is not None else operation.created_at.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created_at
    if age < STALE_GRACE_PERIOD:
        # Could still be genuinely in flight (Fineract itself still
        # processing, or our own request hasn't timed out yet from the
        # caller's point of view) -- don't jump to a conclusion yet.
        return RecoveryResult(202, {"status": "IN_PROGRESS", "external_reference": operation.external_reference})

    # Past the grace period and Fineract has no record of it under this
    # externalId: it genuinely never happened. Safe to report as a clean
    # failure -- the caller can retry with a brand new external_reference.
    detail = {"error": "Fineract has no record of this transaction after the grace period; it did not happen"}
    fail_operation(db, operation, 502, detail)
    logger.warning(
        "Fineract call never landed, marking FAILED",
        extra={**_log_fields(operation, correlation_id), "status": "FAILED"},
    )
    return RecoveryResult(502, detail)


def recover_operation(
    db: Session, fineract: FineractClient, mock_erp: MockErpClient, operation: IdempotentOperation
) -> RecoveryResult:
    """Entry point for the sweep (admin endpoint / startup): dispatch based on
    what this row is actually stuck on."""
    if operation.status == OperationStatus.IN_PROGRESS:
        return recover_unconfirmed_fineract_call(db, fineract, mock_erp, operation)
    if operation.status in (OperationStatus.FINERACT_DONE, OperationStatus.ERP_PENDING, OperationStatus.NEEDS_RECONCILIATION):
        # Fineract's leg is already confirmed done (fineract_transaction_id is
        # set) -- re-fetch its breakdown and retry just the ERP leg. Never
        # calls Fineract's disburse/repay command again.
        assert operation.fineract_loan_id is not None
        assert operation.fineract_transaction_id is not None
        transactions = fineract.list_loan_transactions(operation.fineract_loan_id)
        transaction = next((t for t in transactions if t.get("id") == operation.fineract_transaction_id), None)
        if transaction is None:
            # Shouldn't happen -- the transaction id we recorded came from
            # Fineract itself -- but fail loudly rather than silently stall.
            detail = {"error": f"Fineract transaction {operation.fineract_transaction_id} not found on loan"}
            mark_needs_reconciliation(db, operation, detail)
            return RecoveryResult(202, {"status": "NEEDS_RECONCILIATION", "external_reference": operation.external_reference})
        return _post_to_erp(db, mock_erp, operation, transaction)
    # COMPLETED/FAILED: nothing to recover.
    return RecoveryResult(
        operation.response_status_code or 200, operation.response_body or {"status": operation.status.value}
    )


_STUCK_STATUSES = (
    OperationStatus.IN_PROGRESS,
    OperationStatus.FINERACT_DONE,
    OperationStatus.ERP_PENDING,
    OperationStatus.NEEDS_RECONCILIATION,
)


def run_recovery_sweep(db: Session) -> list[dict[str, Any]]:
    """The "startup sweep, scheduled job, or admin endpoint" the brief says is
    enough ("recovery without a worker is acceptable"). Finds every
    LOAN_DISBURSEMENT/LOAN_REPAYMENT row not yet at a terminal state and
    drives each one forward one step via recover_operation(). Safe to call
    repeatedly and safe to call when there's nothing to do."""
    rows = db.execute(
        select(IdempotentOperation).where(
            IdempotentOperation.operation_type.in_([OperationType.LOAN_DISBURSEMENT, OperationType.LOAN_REPAYMENT]),
            IdempotentOperation.status.in_(_STUCK_STATUSES),
        )
    ).scalars().all()

    fineract = FineractClient()
    mock_erp = MockErpClient()
    results: list[dict[str, Any]] = []
    try:
        for operation in rows:
            before_status = operation.status.value
            result = recover_operation(db, fineract, mock_erp, operation)
            results.append(
                {
                    "external_reference": operation.external_reference,
                    "status_before": before_status,
                    "status_after": operation.status.value,
                    "result_status_code": result.status_code,
                }
            )
            logger.info(
                "recovery sweep processed operation",
                extra={
                    "external_reference": operation.external_reference,
                    "status_before": before_status,
                    "status_after": operation.status.value,
                },
            )
    finally:
        fineract.close()
        mock_erp.close()
    return results
