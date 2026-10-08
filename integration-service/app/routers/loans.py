import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.clients.fineract import FineractClient, FineractError
from app.clients.mock_erp import MockErpClient
from app.db.session import get_db
from app.exceptions import NotFoundError, UpstreamError
from app.fineract_dates import FINERACT_DATE_FORMAT, format_fineract_date
from app.idempotency import (
    IdempotencyDecision,
    RaceLost,
    check_idempotency,
    complete_operation,
    fail_operation,
    retry_scope_operation,
    start_operation,
)
from app.models.idempotent_operation import IdempotentOperation, OperationStatus, OperationType
from app.models.mapping import ClientMapping, LoanMapping
from app.recovery import complete_fineract_leg_and_post_to_erp, recover_unconfirmed_fineract_call
from app.schemas.loan import LoanApplicationCreate, LoanDisburseRequest, LoanRead, LoanRepaymentRequest

router = APIRouter(prefix="/api/loans", tags=["loans"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)

FINERACT_PRODUCT_ID = 1  # "SME Asset Finance", created by scripts/setup-fineract-environment.sh (Part B)


def _loan_mapping_or_404(db: Session, loan_reference: str) -> LoanMapping:
    mapping = db.execute(select(LoanMapping).where(LoanMapping.loan_reference == loan_reference)).scalar_one_or_none()
    if mapping is None:
        raise NotFoundError(f"No loan with loan_reference '{loan_reference}'")
    return mapping


def _reuse_if_failed_scope_match(
    db: Session,
    decision: IdempotencyDecision,
    external_reference: str,
    payload: dict[str, Any],
    loan_reference: str,
    fineract_loan_id: int,
) -> IdempotentOperation | None:
    """A FAILED disbursement match found via scope_key (a *different*
    reference than this request's, for the same loan) must not permanently
    block every future disbursement attempt for that loan -- nothing
    actually happened at Fineract, so there's no business fact to protect.
    Repurposes that row for this fresh attempt. Returns None if this isn't
    that specific case (caller should fall back to the normal replay)."""
    is_failed_scope_match = (
        decision.matched_via == "scope_key"
        and decision.operation is not None
        and decision.operation.status == OperationStatus.FAILED
    )
    if is_failed_scope_match:
        assert decision.operation is not None
        return retry_scope_operation(db, decision.operation, external_reference, payload, loan_reference, fineract_loan_id)
    return None


@router.post("", status_code=201)
def create_loan_application(
    payload: LoanApplicationCreate, response: Response, db: Session = Depends(get_db)
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    correlation_fields = {
        "external_reference": payload.external_reference,
        "client_uid": payload.client_uid,
        "loan_reference": payload.loan_reference,
    }

    decision = check_idempotency(db, OperationType.LOAN_APPLICATION, payload.external_reference, request_payload)
    if not decision.is_new:
        response.status_code = decision.replay_status_code or 200
        return decision.replay_body or {}

    client_mapping = db.execute(select(ClientMapping).where(ClientMapping.client_uid == payload.client_uid)).scalar_one_or_none()
    if client_mapping is None:
        raise NotFoundError(f"No client with client_uid '{payload.client_uid}' -- create the client before applying for a loan")

    try:
        operation = start_operation(db, OperationType.LOAN_APPLICATION, payload.external_reference, request_payload)
    except RaceLost as exc:
        response.status_code = exc.decision.replay_status_code or 200
        return exc.decision.replay_body or {}

    disbursement_date_str = format_fineract_date(payload.expected_disbursement_date)
    fineract_payload = {
        "clientId": client_mapping.fineract_client_id,
        "productId": FINERACT_PRODUCT_ID,
        "loanType": "individual",
        "principal": str(payload.principal),
        "loanTermFrequency": payload.number_of_repayments,
        "loanTermFrequencyType": 2,
        "numberOfRepayments": payload.number_of_repayments,
        "repaymentEvery": 1,
        "repaymentFrequencyType": 2,
        "interestRatePerPeriod": str(payload.interest_rate_per_period),
        "interestType": 0,
        "amortizationType": 1,
        "interestCalculationPeriodType": 1,
        "transactionProcessingStrategyCode": "mifos-standard-strategy",
        "expectedDisbursementDate": disbursement_date_str,
        "submittedOnDate": disbursement_date_str,
        "externalId": payload.loan_reference,
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": "en",
    }
    fineract = FineractClient()
    try:
        create_result = fineract.create_loan(fineract_payload)
        fineract_loan_id = create_result["resourceId"]
        # Part E lists no separate approval endpoint ("approval here or
        # separate" -- the brief's own minimal endpoint list only has
        # application/get/disburse/repayments), so approval happens
        # automatically right after application, not as a caller-visible step.
        fineract.approve_loan(
            fineract_loan_id,
            {
                "approvedOnDate": disbursement_date_str,
                "expectedDisbursementDate": disbursement_date_str,
                "dateFormat": FINERACT_DATE_FORMAT,
                "locale": "en",
            },
        )
    except FineractError as exc:
        fail_operation(db, operation, exc.status_code, {"error": str(exc.body)})
        logger.warning("loan application failed at Fineract", extra={**correlation_fields, "status": "FAILED"})
        raise UpstreamError(f"Fineract loan application failed: {exc.body}") from exc
    finally:
        fineract.close()

    db.add(LoanMapping(loan_reference=payload.loan_reference, fineract_loan_id=fineract_loan_id, client_uid=payload.client_uid))
    db.commit()

    response_body = {"loan_reference": payload.loan_reference, "status": "Approved"}
    complete_operation(db, operation, 201, response_body)
    logger.info("loan application created and approved", extra={**correlation_fields, "status": "COMPLETED"})
    return response_body


@router.get("/{loan_reference}")
def get_loan(loan_reference: str, db: Session = Depends(get_db)) -> LoanRead:
    mapping = _loan_mapping_or_404(db, loan_reference)
    fineract = FineractClient()
    try:
        result = fineract.get_loan(mapping.fineract_loan_id)
    finally:
        fineract.close()
    summary = result.get("summary", {})
    return LoanRead(
        loan_reference=loan_reference,
        status=result["status"]["value"],
        principal_outstanding=summary.get("principalOutstanding"),
        total_outstanding=summary.get("totalOutstanding"),
    )


@router.post("/{loan_reference}/disburse", status_code=201)
def disburse_loan(
    loan_reference: str, payload: LoanDisburseRequest, response: Response, db: Session = Depends(get_db)
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    correlation_fields = {"external_reference": payload.external_reference, "loan_reference": loan_reference}

    # scope_key=loan_reference: Part F requires disbursement be unique *per
    # loan*, independent of which external_reference arrives -- see
    # app/idempotency.py's docstring for the full reasoning.
    decision = check_idempotency(
        db, OperationType.LOAN_DISBURSEMENT, payload.external_reference, request_payload, scope_key=loan_reference
    )
    mapping = _loan_mapping_or_404(db, loan_reference)

    operation: IdempotentOperation
    if decision.is_new:
        try:
            operation = start_operation(
                db,
                OperationType.LOAN_DISBURSEMENT,
                payload.external_reference,
                request_payload,
                scope_key=loan_reference,
                loan_reference=loan_reference,
                fineract_loan_id=mapping.fineract_loan_id,
            )
        except RaceLost as exc:
            reused = _reuse_if_failed_scope_match(
                db, exc.decision, payload.external_reference, request_payload, loan_reference, mapping.fineract_loan_id
            )
            if reused is None:
                response.status_code = exc.decision.replay_status_code or 200
                return exc.decision.replay_body or {}
            operation = reused
    else:
        reused = _reuse_if_failed_scope_match(
            db, decision, payload.external_reference, request_payload, loan_reference, mapping.fineract_loan_id
        )
        if reused is None:
            response.status_code = decision.replay_status_code or 200
            return decision.replay_body or {}
        operation = reused

    # externalId lets a later timeout/crash recovery look this exact
    # transaction up by reference instead of blindly retrying -- Part J3.
    fineract_payload = {
        "actualDisbursementDate": format_fineract_date(payload.actual_disbursement_date),
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": "en",
        "externalId": payload.external_reference,
    }
    fineract = FineractClient()
    mock_erp = MockErpClient()
    try:
        try:
            fineract.disburse_loan(mapping.fineract_loan_id, fineract_payload)
        except httpx.ConnectError as exc:
            # Fineract is unreachable -- unlike a timeout, there's no
            # ambiguity here: the request never left this process, so
            # nothing happened at Fineract. A clean, immediate failure,
            # safe to retry (with a new reference, or the same one once
            # Fineract is back -- see DECISIONS.md, Part J scenario 2).
            fail_operation(db, operation, 503, {"error": f"Fineract unreachable: {exc}"})
            logger.warning("Fineract unreachable", extra={**correlation_fields, "status": "FAILED"})
            raise UpstreamError(f"Fineract unreachable: {exc}") from exc
        except httpx.TimeoutException:
            # We don't know if this landed at Fineract -- look it up by
            # externalId rather than assuming either outcome.
            result = recover_unconfirmed_fineract_call(db, fineract, mock_erp, operation)
            response.status_code = result.status_code
            return result.body
        except FineractError as exc:
            fail_operation(db, operation, exc.status_code, {"error": str(exc.body)})
            logger.warning("disbursement failed at Fineract", extra={**correlation_fields, "status": "FAILED"})
            raise UpstreamError(f"Fineract disbursement failed: {exc.body}") from exc

        transaction = fineract.find_loan_transaction_by_external_id(mapping.fineract_loan_id, payload.external_reference)
        if transaction is None:
            # Fineract returned success but the transaction isn't visible by
            # externalId yet -- treat like the timeout path rather than crash.
            result = recover_unconfirmed_fineract_call(db, fineract, mock_erp, operation)
            response.status_code = result.status_code
            return result.body

        result = complete_fineract_leg_and_post_to_erp(db, mock_erp, operation, transaction)
    finally:
        fineract.close()
        mock_erp.close()

    response.status_code = result.status_code
    logger.info("loan disbursed", extra={**correlation_fields, "status": result.body.get("status")})
    return result.body


@router.post("/{loan_reference}/repayments", status_code=201)
def repay_loan(
    loan_reference: str, payload: LoanRepaymentRequest, response: Response, db: Session = Depends(get_db)
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    correlation_fields = {"external_reference": payload.external_reference, "loan_reference": loan_reference}

    # No scope_key here, deliberately -- unlike disbursement, "many legitimate
    # repayments may hit one loan" (Part F), so uniqueness is purely per
    # external_reference, same as every other non-disbursement operation.
    decision = check_idempotency(db, OperationType.LOAN_REPAYMENT, payload.external_reference, request_payload)
    if not decision.is_new:
        response.status_code = decision.replay_status_code or 200
        return decision.replay_body or {}

    mapping = _loan_mapping_or_404(db, loan_reference)
    try:
        operation = start_operation(
            db,
            OperationType.LOAN_REPAYMENT,
            payload.external_reference,
            request_payload,
            loan_reference=loan_reference,
            fineract_loan_id=mapping.fineract_loan_id,
        )
    except RaceLost as exc:
        response.status_code = exc.decision.replay_status_code or 200
        return exc.decision.replay_body or {}

    fineract_payload = {
        "transactionDate": format_fineract_date(payload.transaction_date),
        "transactionAmount": str(payload.transaction_amount),
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": "en",
        "externalId": payload.external_reference,
    }
    fineract = FineractClient()
    mock_erp = MockErpClient()
    try:
        try:
            fineract.repay_loan(mapping.fineract_loan_id, fineract_payload)
        except httpx.ConnectError as exc:
            fail_operation(db, operation, 503, {"error": f"Fineract unreachable: {exc}"})
            logger.warning("Fineract unreachable", extra={**correlation_fields, "status": "FAILED"})
            raise UpstreamError(f"Fineract unreachable: {exc}") from exc
        except httpx.TimeoutException:
            result = recover_unconfirmed_fineract_call(db, fineract, mock_erp, operation)
            response.status_code = result.status_code
            return result.body
        except FineractError as exc:
            fail_operation(db, operation, exc.status_code, {"error": str(exc.body)})
            logger.warning("repayment failed at Fineract", extra={**correlation_fields, "status": "FAILED"})
            raise UpstreamError(f"Fineract repayment failed: {exc.body}") from exc

        transaction = fineract.find_loan_transaction_by_external_id(mapping.fineract_loan_id, payload.external_reference)
        if transaction is None:
            result = recover_unconfirmed_fineract_call(db, fineract, mock_erp, operation)
            response.status_code = result.status_code
            return result.body

        result = complete_fineract_leg_and_post_to_erp(db, mock_erp, operation, transaction)
    finally:
        fineract.close()
        mock_erp.close()

    response.status_code = result.status_code
    logger.info("repayment applied", extra={**correlation_fields, "status": result.body.get("status")})
    return result.body
