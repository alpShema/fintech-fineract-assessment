from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.db.session import get_db
from app.exceptions import NotFoundError
from app.models.idempotent_operation import IdempotentOperation
from app.schemas.transaction import TransactionRead

router = APIRouter(prefix="/api/transactions", tags=["transactions"], dependencies=[Depends(require_api_key)])


@router.get("/{external_reference}")
def get_transaction(external_reference: str, db: Session = Depends(get_db)) -> TransactionRead:
    """Looks up ANY tracked operation by its external_reference, not just
    loan disbursements/repayments -- answers the observability requirement's
    own example directly: "what happened to FIN-2026-000001?" from one API
    call, regardless of what kind of operation that reference belongs to."""
    operation = db.execute(
        select(IdempotentOperation).where(IdempotentOperation.external_reference == external_reference)
    ).scalar_one_or_none()
    if operation is None:
        raise NotFoundError(f"No operation found with external_reference '{external_reference}'")
    return TransactionRead(
        external_reference=external_reference,
        operation_type=operation.operation_type.value,
        status=operation.status.value,
        result=operation.response_body,
    )
