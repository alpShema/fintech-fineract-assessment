"""Part K: reconciliation compares Fineract and the ERP over a date range or
set of references.

Keying: per the brief, "compare per Fineract transaction (disbursement or
repayment), matched to the ERP journal entry carrying the same
external_reference." Fineract has no bulk transactions-by-date-range
endpoint in this version, so the scope is every loan this Integration
Service has ever touched (`LoanMapping` -- the loans it created), each
queried via the single `GET /loans/{id}?associations=transactions` call
that already returns every transaction's full breakdown in one shot (same
endpoint Part H and Part J3 already use).

Reversals: a Fineract transaction with `manuallyReversed: true` is excluded
from normal matching and reported under its own REVERSED bucket -- matching
it against a journal entry would compare a stale amount against something
that no longer reflects reality. A real reversal would need its own
reversing journal entry (DR/CR swapped, a derived reference like
`{original}-REVERSAL`); that posting isn't built here, same as Part H's
overpayment handling -- documented, not demonstrated, since the brief marks
this read-only and master-data reconciliation a stretch item.

Read-only: this endpoint never writes anything, to Fineract, the ERP, or its
own database. Any future repair action must be explicit and idempotent, per
the brief -- not built here.
"""

import enum
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.clients.fineract import FineractClient
from app.clients.mock_erp import MockErpClient
from app.db.session import get_db
from app.models.mapping import LoanMapping

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"], dependencies=[Depends(require_api_key)])

_TRANSACTION_TYPES = {"loanTransactionType.disbursement", "loanTransactionType.repayment"}


class ReconciliationRequest(BaseModel):
    date_from: date | None = None
    date_to: date | None = None
    external_references: list[str] | None = None


class ReconciliationStatus(str, enum.Enum):
    MATCHED = "MATCHED"
    MISSING_IN_ERP = "MISSING_IN_ERP"
    MISSING_IN_FINERACT = "MISSING_IN_FINERACT"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    REVERSED = "REVERSED"


class ReconciliationItem(BaseModel):
    external_reference: str
    status: ReconciliationStatus
    fineract_amount: Decimal | None = None
    erp_amount: Decimal | None = None


class ReconciliationReport(BaseModel):
    items: list[ReconciliationItem]
    summary: dict[str, int]


def _within_range(transaction_date: date, date_from: date | None, date_to: date | None) -> bool:
    if date_from is not None and transaction_date < date_from:
        return False
    if date_to is not None and transaction_date > date_to:
        return False
    return True


def _parse_fineract_date(value: list[int]) -> date:
    year, month, day = value
    return date(year, month, day)


def _collect_fineract_transactions(
    db: Session, fineract: FineractClient, payload: ReconciliationRequest
) -> dict[str, dict[str, Any]]:
    """external_reference -> transaction, for every non-reversed
    disbursement/repayment across every loan this service knows about,
    filtered to the requested scope."""
    loans = db.execute(select(LoanMapping)).scalars().all()
    by_reference: dict[str, dict[str, Any]] = {}
    for loan in loans:
        for transaction in fineract.list_loan_transactions(loan.fineract_loan_id):
            external_id = transaction.get("externalId")
            if not external_id or transaction["type"]["code"] not in _TRANSACTION_TYPES:
                continue
            if payload.external_references is not None and external_id not in payload.external_references:
                continue
            if not _within_range(_parse_fineract_date(transaction["date"]), payload.date_from, payload.date_to):
                continue
            by_reference[external_id] = transaction
    return by_reference


def _collect_erp_entries(mock_erp: MockErpClient, payload: ReconciliationRequest) -> dict[str, dict[str, Any]]:
    """external_reference -> journal entry, scoped the same way."""
    if payload.external_references is not None:
        entries = []
        for ref in payload.external_references:
            entry = mock_erp.get_journal_entry(ref)
            if entry is not None:
                entries.append(entry)
    else:
        date_from = payload.date_from.isoformat() if payload.date_from else None
        date_to = payload.date_to.isoformat() if payload.date_to else None
        entries = mock_erp.list_journal_entries(date_from, date_to)
    return {entry["external_reference"]: entry for entry in entries}


@router.post("")
def reconcile(
    payload: ReconciliationRequest, db: Session = Depends(get_db)
) -> ReconciliationReport:
    fineract = FineractClient()
    mock_erp = MockErpClient()
    try:
        fineract_by_ref = _collect_fineract_transactions(db, fineract, payload)
        erp_by_ref = _collect_erp_entries(mock_erp, payload)
    finally:
        fineract.close()
        mock_erp.close()

    items: list[ReconciliationItem] = []
    for reference in sorted(set(fineract_by_ref) | set(erp_by_ref)):
        transaction = fineract_by_ref.get(reference)
        entry = erp_by_ref.get(reference)

        if transaction is not None and transaction.get("manuallyReversed"):
            items.append(ReconciliationItem(external_reference=reference, status=ReconciliationStatus.REVERSED))
            continue

        if transaction is None:
            items.append(
                ReconciliationItem(
                    external_reference=reference,
                    status=ReconciliationStatus.MISSING_IN_FINERACT,
                    erp_amount=_entry_amount(entry) if entry else None,
                )
            )
            continue

        if entry is None:
            items.append(
                ReconciliationItem(
                    external_reference=reference,
                    status=ReconciliationStatus.MISSING_IN_ERP,
                    fineract_amount=Decimal(str(transaction["amount"])),
                )
            )
            continue

        fineract_amount = Decimal(str(transaction["amount"]))
        erp_amount = _entry_amount(entry)
        status = ReconciliationStatus.MATCHED if fineract_amount == erp_amount else ReconciliationStatus.AMOUNT_MISMATCH
        items.append(
            ReconciliationItem(
                external_reference=reference, status=status, fineract_amount=fineract_amount, erp_amount=erp_amount
            )
        )

    summary = {status.value: sum(1 for item in items if item.status == status) for status in ReconciliationStatus}
    return ReconciliationReport(items=items, summary=summary)


def _entry_amount(entry: dict[str, Any]) -> Decimal:
    """The entry's overall magnitude -- total debit (== total credit, Mock
    ERP enforces balance) across its lines. Equals the Fineract transaction's
    `amount` for both disbursement and repayment entries under Part H's
    mapping, so it's the natural single number to compare against."""
    return sum((Decimal(str(line["debit"])) for line in entry["lines"]), Decimal("0"))
