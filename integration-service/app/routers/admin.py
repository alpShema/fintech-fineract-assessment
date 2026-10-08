"""Part J: "build a way to inject failures on demand" + the recovery sweep
("recovery without a worker is acceptable ... an admin endpoint is enough").
Test/demo-only surface -- gated behind the same API key as every other
route, not a production admin API."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import fault_injection
from app.auth import require_api_key
from app.db.session import get_db
from app.recovery import run_recovery_sweep

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_api_key)])


class FaultInjectionArmRequest(BaseModel):
    target: str
    mode: str
    times: int = 1


@router.post("/fault-injection", status_code=201)
def arm_fault(payload: FaultInjectionArmRequest) -> dict[str, Any]:
    try:
        fault_injection.arm(payload.target, payload.mode, payload.times)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"armed": fault_injection.status()}


@router.get("/fault-injection")
def fault_status() -> dict[str, Any]:
    return {"armed": fault_injection.status()}


@router.delete("/fault-injection")
def clear_faults(target: str | None = None) -> dict[str, Any]:
    fault_injection.clear(target)
    return {"armed": fault_injection.status()}


@router.post("/recover")
def recover(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Drives every stuck LOAN_DISBURSEMENT/LOAN_REPAYMENT operation one step
    forward. Also run once automatically at app startup -- see main.py's
    lifespan -- so a container restart recovers on its own without this ever
    needing to be called manually, though it's safe (and sometimes
    necessary, e.g. to retry a still-down ERP) to call again."""
    results = run_recovery_sweep(db)
    return {"processed": len(results), "results": results}
