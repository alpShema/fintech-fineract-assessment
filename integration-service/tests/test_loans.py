import respx
from fastapi.testclient import TestClient
from httpx import Response
from respx import Route
from sqlalchemy.orm import Session

from app.config import settings
from app.models.mapping import ClientMapping, LoanMapping
from tests.conftest import API_KEY_HEADERS


def _seed_client(db_session: Session, client_uid: str = "SME-000001", fineract_client_id: int = 7) -> None:
    db_session.add(ClientMapping(client_uid=client_uid, fineract_client_id=fineract_client_id))
    db_session.commit()


def _seed_loan(
    db_session: Session, loan_reference: str = "LN-1", fineract_loan_id: int = 3, client_uid: str = "SME-000001"
) -> None:
    db_session.add(LoanMapping(loan_reference=loan_reference, fineract_loan_id=fineract_loan_id, client_uid=client_uid))
    db_session.commit()


def _loan_payload(**overrides: object) -> dict:
    base = {
        "external_reference": "REF-L1",
        "client_uid": "SME-000001",
        "loan_reference": "LN-1",
        "principal": "5000000",
        "expected_disbursement_date": "2026-01-15",
    }
    base.update(overrides)
    return base


def _mock_transactions_lookup(fineract_loan_id: int, transactions: list[dict]) -> None:
    """Mocks the GET .../loans/{id}?associations=transactions call every
    disburse/repay now makes to fetch Fineract's authoritative breakdown
    (Part H) -- `transactions` just needs an entry per externalId the test
    will look up; extras are harmless since the lookup filters client-side."""
    respx.get(f"{settings.fineract_base_url}/loans/{fineract_loan_id}", params={"associations": "transactions"}).mock(
        return_value=Response(200, json={"transactions": transactions})
    )


def _mock_erp_journal_entry() -> Route:
    return respx.post(f"{settings.mock_erp_base_url}/api/journal-entries").mock(
        return_value=Response(201, json={"id": 1, "external_reference": "whatever"})
    )


def _disbursement_transaction(transaction_id: int, external_id: str, amount: float = 5000000.0) -> dict:
    return {
        "id": transaction_id,
        "externalId": external_id,
        "amount": amount,
        "principalPortion": 0,
        "interestPortion": 0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "date": [2026, 1, 15],
    }


def _repayment_transaction(transaction_id: int, external_id: str) -> dict:
    return {
        "id": transaction_id,
        "externalId": external_id,
        "amount": 458400.0,
        "principalPortion": 400000.0,
        "interestPortion": 58400.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
        "date": [2026, 2, 15],
    }


@respx.mock
def test_loan_application_without_client_returns_404(client: TestClient) -> None:
    resp = client.post("/api/loans", json=_loan_payload(), headers=API_KEY_HEADERS)
    assert resp.status_code == 404


@respx.mock
def test_loan_application_creates_and_auto_approves(client: TestClient, db_session: Session) -> None:
    _seed_client(db_session)
    create_route = respx.post(f"{settings.fineract_base_url}/loans").mock(return_value=Response(200, json={"resourceId": 3}))
    approve_route = respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "approve"}).mock(
        return_value=Response(200, json={"resourceId": 3})
    )

    resp = client.post("/api/loans", json=_loan_payload(), headers=API_KEY_HEADERS)
    assert resp.status_code == 201
    assert resp.json() == {"loan_reference": "LN-1", "status": "Approved"}
    assert create_route.call_count == 1
    assert approve_route.call_count == 1


@respx.mock
def test_resend_identical_application_does_not_call_fineract_again(client: TestClient, db_session: Session) -> None:
    _seed_client(db_session)
    create_route = respx.post(f"{settings.fineract_base_url}/loans").mock(return_value=Response(200, json={"resourceId": 3}))
    respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "approve"}).mock(return_value=Response(200, json={}))

    client.post("/api/loans", json=_loan_payload(), headers=API_KEY_HEADERS)
    second = client.post("/api/loans", json=_loan_payload(), headers=API_KEY_HEADERS)
    assert second.status_code == 200
    assert create_route.call_count == 1


@respx.mock
def test_get_loan(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    respx.get(f"{settings.fineract_base_url}/loans/3").mock(
        return_value=Response(
            200,
            json={
                "status": {"value": "Active"},
                "summary": {"principalOutstanding": "5000000.00", "totalOutstanding": "5500000.00"},
            },
        )
    )
    resp = client.get("/api/loans/LN-1", headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "Active"
    assert resp.json()["principal_outstanding"] == "5000000.00"


@respx.mock
def test_disburse_loan(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "disburse"}).mock(
        return_value=Response(200, json={"subResourceId": 55})
    )
    _mock_transactions_lookup(3, [_disbursement_transaction(55, "DISB-1")])
    journal_route = _mock_erp_journal_entry()
    resp = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-1", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["fineract_transaction_id"] == 55
    assert resp.json()["erp_transaction_id"] == 1  # Mock ERP's own journal-entry id, not just Fineract's
    assert resp.json()["status"] == "Active"
    assert journal_route.call_count == 1
    posted = journal_route.calls.last.request.content
    assert b'"DISB-1"' in posted  # ERP entry keyed by the same external_reference


@respx.mock
def test_second_disbursement_different_reference_same_loan_replays_first_result(client: TestClient, db_session: Session) -> None:
    """The brief's exact scenario: concurrent/sequential disbursement requests
    with DIFFERENT external_reference values for the SAME loan must still
    result in exactly one Fineract disbursement call."""
    _seed_loan(db_session)
    disburse_route = respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "disburse"}).mock(
        return_value=Response(200, json={"subResourceId": 55})
    )
    _mock_transactions_lookup(3, [_disbursement_transaction(55, "DISB-A")])
    _mock_erp_journal_entry()

    first = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-A", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert first.status_code == 201

    second = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-B", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert second.status_code == 200  # replayed result (Part F: replay is always 200), not an error
    assert second.json() == first.json()
    assert disburse_route.call_count == 1  # Fineract only ever disbursed once


@respx.mock
def test_two_different_repayment_references_both_apply(client: TestClient, db_session: Session) -> None:
    _seed_loan(db_session)
    repay_route = respx.post(f"{settings.fineract_base_url}/loans/3/transactions", params={"command": "repayment"}).mock(
        side_effect=[Response(200, json={"resourceId": 1}), Response(200, json={"resourceId": 2})]
    )
    _mock_transactions_lookup(
        3, [_repayment_transaction(1, "REPAY-1"), _repayment_transaction(2, "REPAY-2")]
    )
    journal_route = _mock_erp_journal_entry()
    for ref in ("REPAY-1", "REPAY-2"):
        resp = client.post(
            "/api/loans/LN-1/repayments",
            json={"external_reference": ref, "transaction_date": "2026-02-15", "transaction_amount": "458400"},
            headers=API_KEY_HEADERS,
        )
        assert resp.status_code == 201
    assert repay_route.call_count == 2  # both legitimate, both applied
    assert journal_route.call_count == 2  # one ERP journal entry per repayment


@respx.mock
def test_disburse_when_fineract_unreachable_returns_clean_failure(client: TestClient, db_session: Session) -> None:
    """Part J scenario 2: Fineract down. httpx.ConnectError means the request
    never left this process -- no ambiguity, so this must be a clean, quick
    failure (not an unhandled 500, and not the timeout/lookup recovery path,
    which is for when we genuinely don't know what happened)."""
    import httpx

    _seed_loan(db_session)
    respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "disburse"}).mock(
        side_effect=httpx.ConnectError("simulated: fineract down")
    )
    resp = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-DOWN-1", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert resp.status_code == 502
    assert resp.json()["error_code"] == "UPSTREAM_ERROR"


@respx.mock
def test_disburse_unreachable_then_retry_with_new_reference_succeeds(client: TestClient, db_session: Session) -> None:
    """Nothing was persisted at Fineract (confirmed: loan state never
    changed), so a fresh external_reference for the same loan must be able
    to proceed normally once Fineract is back."""
    import httpx

    _seed_loan(db_session)
    disburse_route = respx.post(f"{settings.fineract_base_url}/loans/3", params={"command": "disburse"}).mock(
        side_effect=[httpx.ConnectError("simulated: fineract down"), Response(200, json={"subResourceId": 55})]
    )
    _mock_transactions_lookup(3, [_disbursement_transaction(55, "DISB-DOWN-RETRY")])
    _mock_erp_journal_entry()

    first = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-DOWN-A", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert first.status_code == 502

    second = client.post(
        "/api/loans/LN-1/disburse",
        json={"external_reference": "DISB-DOWN-RETRY", "actual_disbursement_date": "2026-01-15"},
        headers=API_KEY_HEADERS,
    )
    assert second.status_code == 201
    assert disburse_route.call_count == 2
