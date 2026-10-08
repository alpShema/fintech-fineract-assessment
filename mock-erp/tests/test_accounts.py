from fastapi.testclient import TestClient


def _payload(**overrides: object) -> dict[str, object]:
    base = {"code": "1200", "name": "Loan Receivable", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1200"}
    base.update(overrides)
    return base


def test_create_account_returns_201(client: TestClient) -> None:
    resp = client.post("/api/accounts", json=_payload())
    assert resp.status_code == 201
    body = resp.json()
    assert body["code"] == "1200"
    assert body["external_reference"] == "GL-1200"


def test_resend_identical_payload_returns_200_same_account(client: TestClient) -> None:
    first = client.post("/api/accounts", json=_payload())
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = client.post("/api/accounts", json=_payload())
    assert second.status_code == 200
    assert second.json()["id"] == first_id


def test_same_reference_different_payload_returns_409(client: TestClient) -> None:
    client.post("/api/accounts", json=_payload())
    resp = client.post("/api/accounts", json=_payload(name="Loan Receivable RENAMED"))
    assert resp.status_code == 409


def test_duplicate_code_different_reference_returns_409(client: TestClient) -> None:
    client.post("/api/accounts", json=_payload())
    resp = client.post("/api/accounts", json=_payload(external_reference="GL-DIFFERENT"))
    assert resp.status_code == 409


def test_list_accounts(client: TestClient) -> None:
    client.post("/api/accounts", json=_payload())
    client.post("/api/accounts", json=_payload(code="1100", name="Bank", external_reference="GL-1100"))
    resp = client.get("/api/accounts")
    assert resp.status_code == 200
    codes = [a["code"] for a in resp.json()]
    assert codes == ["1100", "1200"]  # ordered by code


def test_balance_for_asset_account_is_debit_minus_credit(client: TestClient) -> None:
    client.post("/api/accounts", json=_payload(code="1200", name="Loan Receivable", external_reference="GL-1200"))
    client.post("/api/accounts", json=_payload(code="1100", name="Bank", type="ASSET", external_reference="GL-1100"))
    client.post(
        "/api/journal-entries",
        json={
            "external_reference": "JE-1",
            "entry_date": "2026-01-15",
            "description": "disbursement",
            "lines": [
                {"account_code": "1200", "debit": "5000000.00"},
                {"account_code": "1100", "credit": "5000000.00"},
            ],
        },
    )
    resp = client.get("/api/accounts/1200/balance")
    assert resp.status_code == 200
    assert resp.json()["balance"] == "5000000.00"

    bank_resp = client.get("/api/accounts/1100/balance")
    assert bank_resp.json()["balance"] == "-5000000.00"


def test_balance_for_unknown_account_returns_404(client: TestClient) -> None:
    resp = client.get("/api/accounts/9999/balance")
    assert resp.status_code == 404
