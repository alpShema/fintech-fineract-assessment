import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.account_type_mapping import map_erp_account_type_to_fineract
from app.auth import require_api_key
from app.clients.fineract import FineractClient, FineractError
from app.clients.mock_erp import MockErpClient
from app.db.session import get_db
from app.idempotency import check_idempotency, complete_operation, fail_operation, retry_operation, start_operation
from app.models.idempotent_operation import IdempotentOperation, OperationStatus, OperationType
from app.schemas.account_sync import AccountSyncRequest, AccountSyncResponse, AccountSyncResult

router = APIRouter(prefix="/api/accounts", tags=["accounts"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)


def _sync_one_account(db: Session, fineract: FineractClient, erp_account: dict[str, Any]) -> AccountSyncResult:
    external_reference = erp_account["external_reference"]
    code = erp_account["code"]

    fineract_payload = {
        "name": erp_account["name"],
        "glCode": code,
        "type": map_erp_account_type_to_fineract(erp_account["type"]),
        "usage": 1,
        "manualEntriesAllowed": True,
        "externalReference": external_reference,
    }

    decision = check_idempotency(db, OperationType.GL_ACCOUNT_SYNC, external_reference, fineract_payload)
    operation: IdempotentOperation
    if decision.is_new:
        operation = start_operation(db, OperationType.GL_ACCOUNT_SYNC, external_reference, fineract_payload)
    elif decision.operation is not None and decision.operation.status == OperationStatus.FAILED:
        # A previous attempt failed (e.g. Fineract was down) -- retry for
        # real rather than replaying that same cached failure forever.
        operation = decision.operation
        retry_operation(db, operation)
    else:
        body = decision.replay_body or {}
        is_synced = decision.replay_status_code in (200, 201)
        return AccountSyncResult(
            code=code,
            external_reference=external_reference,
            status="SYNCED" if is_synced else "FAILED",
            fineract_gl_account_id=body.get("resourceId") if is_synced else None,
            error=None if is_synced else body.get("error"),
        )

    try:
        result = fineract.create_gl_account(fineract_payload)
    except FineractError as exc:
        fail_operation(db, operation, exc.status_code, {"error": str(exc.body)})
        logger.warning(
            "GL account sync failed", extra={"external_reference": external_reference, "status": "FAILED"}
        )
        return AccountSyncResult(code=code, external_reference=external_reference, status="FAILED", error=str(exc.body))

    complete_operation(db, operation, 201, result)
    logger.info("GL account synced", extra={"external_reference": external_reference, "status": "SYNCED"})
    return AccountSyncResult(
        code=code, external_reference=external_reference, status="SYNCED", fineract_gl_account_id=result.get("resourceId")
    )


@router.post("/sync")
def sync_accounts(payload: AccountSyncRequest, db: Session = Depends(get_db)) -> AccountSyncResponse:
    mock_erp = MockErpClient()
    fineract = FineractClient()
    try:
        erp_accounts = mock_erp.list_accounts()
        if payload.account_code:
            erp_accounts = [a for a in erp_accounts if a["code"] == payload.account_code]
        results = [_sync_one_account(db, fineract, account) for account in erp_accounts]
    finally:
        mock_erp.close()
        fineract.close()
    return AccountSyncResponse(results=results)
