import respx
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.orm import Session

from app.config import settings
from app.models.mapping import LoanMapping
from tests.conftest import API_KEY_HEADERS


def _seed_loan(db_session: Session, loan_reference: str = "LN-1", fineract_loan_id: int = 3) -> None:
    db_session.add(LoanMapping(loan_reference=loan_reference, fineract_loan_id=fineract_loan_id, client_uid="SME-1"))
    db_session.commit()


def _txn(external_id: str, amount: float, code: str = "loanTransactionType.disbursement", reversed_: bool = False) -> dict:
    return {
        "externalId": external_id,
        "amount": amount,
        "type": {"code": code},
        "date": [2026, 1, 15],
        "manuallyReversed": reversed_,
    }


def _entry(external_reference: str, debit: str) -> dict:
    return {
        "external_reference": external_reference,
        "entry_date": "2026-01-15",
        "lines": [
            {"account_code": "1200", "debit": debit, "credit": "0"},
            {"account_code": "1100", "debit": "0", "credit": debit},
        ],
    }


@respx.mock
def test_all_four_outcomes_plus_reversed(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    respx.get(f"{settings.fineract_base_url}/loans/3", params={"associations": "transactions"}).mock(
        return_value=Response(
            200,
            json={
                "transactions": [
                    _txn("REF-MATCHED", 100000.0),
                    _txn("REF-MISSING-ERP", 200000.0),
                    _txn("REF-MISMATCH", 300000.0),
                    _txn("REF-REVERSED", 400000.0, reversed_=True),
                ]
            },
        )
    )
    respx.get(f"{settings.mock_erp_base_url}/api/journal-entries").mock(
        return_value=Response(
            200,
            json=[
                _entry("REF-MATCHED", "100000.00"),
                _entry("REF-MISMATCH", "999999.00"),
                _entry("REF-MISSING-FINERACT", "50000.00"),
            ],
        )
    )

    resp = client.post("/api/reconciliation", json={"date_from": "2026-01-01", "date_to": "2026-01-31"}, headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    by_ref = {item["external_reference"]: item for item in body["items"]}

    assert by_ref["REF-MATCHED"]["status"] == "MATCHED"
    assert by_ref["REF-MISSING-ERP"]["status"] == "MISSING_IN_ERP"
    assert by_ref["REF-MISSING-FINERACT"]["status"] == "MISSING_IN_FINERACT"
    assert by_ref["REF-MISMATCH"]["status"] == "AMOUNT_MISMATCH"
    assert by_ref["REF-REVERSED"]["status"] == "REVERSED"

    assert body["summary"] == {
        "MATCHED": 1,
        "MISSING_IN_ERP": 1,
        "MISSING_IN_FINERACT": 1,
        "AMOUNT_MISMATCH": 1,
        "REVERSED": 1,
    }


@respx.mock
def test_scoped_to_explicit_references(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    respx.get(f"{settings.fineract_base_url}/loans/3", params={"associations": "transactions"}).mock(
        return_value=Response(200, json={"transactions": [_txn("REF-A", 1000.0), _txn("REF-B", 2000.0)]})
    )
    respx.get(f"{settings.mock_erp_base_url}/api/journal-entries/REF-A").mock(
        return_value=Response(200, json=_entry("REF-A", "1000.00"))
    )

    resp = client.post("/api/reconciliation", json={"external_references": ["REF-A"]}, headers=API_KEY_HEADERS)
    body = resp.json()
    refs = {item["external_reference"] for item in body["items"]}
    assert refs == {"REF-A"}  # REF-B excluded, not in scope
    assert body["summary"]["MATCHED"] == 1


@respx.mock
def test_accrual_and_other_transaction_types_are_ignored(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    respx.get(f"{settings.fineract_base_url}/loans/3", params={"associations": "transactions"}).mock(
        return_value=Response(
            200,
            json={
                "transactions": [
                    _txn("REF-DISB", 1000.0),
                    {
                        "externalId": None,
                        "amount": 5.0,
                        "type": {"code": "loanTransactionType.accrual"},
                        "date": [2026, 1, 15],
                        "manuallyReversed": False,
                    },
                ]
            },
        )
    )
    respx.get(f"{settings.mock_erp_base_url}/api/journal-entries").mock(return_value=Response(200, json=[]))

    resp = client.post("/api/reconciliation", json={}, headers=API_KEY_HEADERS)
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["external_reference"] == "REF-DISB"
