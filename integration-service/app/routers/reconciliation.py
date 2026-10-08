from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_api_key

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"], dependencies=[Depends(require_api_key)])


class ReconciliationRequest(BaseModel):
    """Full reconciliation logic (MATCHED / MISSING_IN_ERP / MISSING_IN_FINERACT
    / AMOUNT_MISMATCH across a date range or set of references) is Part K --
    Tier 2 Reliability, not yet built. This endpoint exists now so the API
    surface matches Part E's minimum endpoint list and is documented in
    OpenAPI, but returns a placeholder until Part K lands."""

    date_from: str | None = None
    date_to: str | None = None
    external_references: list[str] | None = None


@router.post("")
def reconcile(payload: ReconciliationRequest) -> dict[str, Any]:
    return {"status": "NOT_YET_IMPLEMENTED", "note": "Full reconciliation logic is Part K (Tier 2 Reliability)."}
