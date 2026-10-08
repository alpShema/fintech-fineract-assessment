from sqlalchemy.orm import Session

from app.idempotency import (
    IdempotencyConflict,
    OperationStatus,
    OperationType,
    RaceLost,
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


def test_start_operation_losing_the_insert_race_raises_race_lost(db_session: Session) -> None:
    """Simulates what real concurrency does: two requests both pass
    check_idempotency()'s is_new=True check (neither row exists yet), then
    both call start_operation(). The first commits; the second hits the DB's
    own unique constraint and must get RaceLost (carrying the same decision
    check_idempotency() would have given), not an unhandled IntegrityError --
    every router that calls start_operation() must catch this (see Part I's
    DECISIONS.md writeup on why the DB constraint, not the pre-check, is the
    actual enforcement layer)."""
    start_operation(db_session, OperationType.LOAN_APPLICATION, "REF-RACE", {"a": 1})
    try:
        start_operation(db_session, OperationType.LOAN_APPLICATION, "REF-RACE", {"a": 1})
        raise AssertionError("expected RaceLost")
    except RaceLost as exc:
        assert exc.decision.is_new is False
        assert exc.decision.replay_status_code == 202  # the winner's row is still IN_PROGRESS at this point


def test_start_operation_losing_the_scope_key_race_raises_race_lost(db_session: Session) -> None:
    """Same race, but for disbursement's scope_key axis: two different
    external_reference values racing for the same loan."""
    start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "REF-A", {"amount": 100}, scope_key="LOAN-RACE")
    try:
        start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "REF-B", {"amount": 100}, scope_key="LOAN-RACE")
        raise AssertionError("expected RaceLost")
    except RaceLost as exc:
        assert exc.decision.is_new is False


def test_failed_disbursement_matched_via_scope_key_is_flagged_for_reuse_not_replay(db_session: Session) -> None:
    """The bug found while verifying Part J scenario 2 live: a FAILED
    disbursement (e.g. Fineract was unreachable) must not permanently brick
    every future disbursement attempt for that loan just because the
    uq_operation_scope_key constraint allows only one row per loan ever.
    check_idempotency() itself still reports is_new=False (the row exists),
    but matched_via="scope_key" + status FAILED is the signal the router
    (disburse_loan) uses to repurpose the row via retry_scope_operation()
    instead of blindly replaying the old failure forever."""
    from app.idempotency import fail_operation

    op = start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "DISB-A", {"amount": 100}, scope_key="LOAN-1")
    fail_operation(db_session, op, 503, {"error": "Fineract unreachable"})

    decision = check_idempotency(db_session, OperationType.LOAN_DISBURSEMENT, "DISB-B", {"amount": 100}, scope_key="LOAN-1")
    assert decision.is_new is False
    assert decision.matched_via == "scope_key"
    assert decision.operation is not None
    assert decision.operation.status == OperationStatus.FAILED


def test_retry_scope_operation_repurposes_the_row_for_the_new_reference(db_session: Session) -> None:
    from app.idempotency import fail_operation, retry_scope_operation

    op = start_operation(db_session, OperationType.LOAN_DISBURSEMENT, "DISB-A", {"amount": 100}, scope_key="LOAN-1")
    fail_operation(db_session, op, 503, {"error": "Fineract unreachable"})

    reused = retry_scope_operation(db_session, op, "DISB-B", {"amount": 100}, "LOAN-1", 42)
    assert reused.external_reference == "DISB-B"
    assert reused.status == OperationStatus.IN_PROGRESS
    assert reused.response_status_code is None
    assert reused.fineract_loan_id == 42

    # The old reference no longer has any row of its own to find -- it was
    # repurposed, not duplicated.
    stale = check_idempotency(db_session, OperationType.LOAN_DISBURSEMENT, "DISB-A", {"amount": 100}, scope_key="LOAN-1")
    assert stale.is_new is False  # finds the (now-repurposed) row via scope_key
    assert stale.operation is not None
    assert stale.operation.external_reference == "DISB-B"


def test_failed_operation_replays_its_failure(db_session: Session) -> None:
    from app.idempotency import fail_operation

    operation = start_operation(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    fail_operation(db_session, operation, 502, {"error": "upstream down"})

    decision = check_idempotency(db_session, OperationType.CLIENT_CREATE, "REF-1", {"a": 1})
    assert decision.is_new is False
    assert decision.replay_status_code == 502
