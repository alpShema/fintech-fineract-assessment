from sqlalchemy.orm import Session

from app.idempotency import (
    IdempotencyConflict,
    OperationType,
    check_idempotency,
    complete_operation,
    start_operation,
)


def test_new_reference_is_new(db_session: Session) -> None:
    decision = check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    assert decision.is_new is True


def test_same_reference_same_payload_in_progress_returns_202(db_session: Session) -> None:
    start_operation(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    decision = check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    assert decision.is_new is False
    assert decision.replay_status_code == 202


def test_same_reference_same_payload_completed_returns_original_result(db_session: Session) -> None:
    operation = start_operation(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    complete_operation(db_session, operation, 201, {"resourceId": 42})

    decision = check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    assert decision.is_new is False
    assert decision.replay_status_code == 200  # Part F: replay is always 200, regardless of the original create status
    assert decision.replay_body == {"resourceId": 42}


def test_same_reference_different_payload_raises_conflict(db_session: Session) -> None:
    start_operation(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    try:
        check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 2})
        raise AssertionError("expected IdempotencyConflict")
    except IdempotencyConflict:
        pass


def test_different_operation_types_do_not_collide(db_session: Session) -> None:
    """Same external_reference string, different operation_type -- these are
    different idempotency keys entirely (the uniqueness scope in Part F is
    explicitly per-operation-type: "GL account: unique per external_reference
    ... Loan application: unique per external_reference" -- as separate rules,
    not one global namespace)."""
    start_operation(db_session, OperationType.CLIENT_CREATE, "REF-SHARED", {"a": 1})
    decision = check_idempotency(db_session, OperationType.LOAN_APPLICATION, "REF-SHARED", {"b": 2})
    assert decision.is_new is True


def test_disbursement_scope_key_blocks_different_reference_same_loan(db_session: Session) -> None:
    """The brief's specific requirement: 'concurrent requests with different
    references for the same loan still result in exactly one Fineract
    disbursement.' First disbursement completes under ref A; a second
    disbursement attempt under a completely different ref B, for the same
    loan (scope_key), must NOT proceed -- it should replay the first result."""
    op = start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "REF-A", {"amount": 100}, scope_key="LOAN-1")
    complete_operation(db_session, op, 201, {"fineract_transaction_id": 999})

    decision = check_idempotency(db_session, OperationType.LOAN_DISBURSEMENT, "REF-B", {"amount": 100}, scope_key="LOAN-1")
    assert decision.is_new is False
    assert decision.replay_body == {"fineract_transaction_id": 999}


def test_disbursement_different_loan_is_independent(db_session: Session) -> None:
    op = start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "REF-A", {"amount": 100}, scope_key="LOAN-1")
    complete_operation(db_session, op, 201, {"fineract_transaction_id": 999})

    decision = check_idempotency(db_session, OperationType.LOAN_DISBURSEMENT, "REF-C", {"amount": 200}, scope_key="LOAN-2")
    assert decision.is_new is True


def test_repayment_has_no_scope_key_many_legitimate_repayments_per_loan(db_session: Session) -> None:
    """Unlike disbursement, repayment has no scope_key -- "many legitimate
    repayments may hit one loan" per the brief, so two different references
    for the same loan must both be able to proceed independently."""
    op1 = start_operation(db_session, OperationType.LOAN_REPAYMENT, "REPAY-1", {"amount": 100})
    complete_operation(db_session, op1, 201, {"fineract_transaction_id": 1})

    decision = check_idempotency(db_session, OperationType.LOAN_REPAYMENT, "REPAY-2", {"amount": 100})
    assert decision.is_new is True


def test_failed_operation_replays_its_failure(db_session: Session) -> None:
    from app.idempotency import fail_operation

    operation = start_operation(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    fail_operation(db_session, operation, 502, {"error": "upstream down"})

    decision = check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    assert decision.is_new is False
    assert decision.replay_status_code == 502
