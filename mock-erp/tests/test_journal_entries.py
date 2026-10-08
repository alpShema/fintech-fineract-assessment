from fastapi.testclient import TestClient


def _seed_accounts(client: TestClient) -> None:
    client.post(
        "/api/accounts",
        json={"code": "1200", "name": "Loan Receivable", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1200"},
    )
    client.post(
        "/api/accounts",
        json={"code": "1100", "name": "Bank", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1100"},
    )
    client.post(
        "/api/accounts",
        json={"code": "4000", "name": "Interest Income", "type": "INCOME", "currency": "RWF", "external_reference": "GL-4000"},
    )


def _balanced_entry(**overrides: object) -> dict[str, object]:
    base = {
        "external_reference": "JE-1",
        "entry_date": "2026-01-15",
        "description": "disbursement",
        "lines": [
            {"account_code": "1200", "debit": "5000000.00"},
            {"account_code": "1100", "credit": "5000000.00"},
        ],
    }
    base.update(overrides)
    return base


def test_create_balanced_entry_returns_201(client: TestClient) -> None:
    _seed_accounts(client)
    resp = client.post("/api/journal-entries", json=_balanced_entry())
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["lines"]) == 2


def test_unbalanced_entry_is_rejected(client: TestClient) -> None:
    _seed_accounts(client)
    unbalanced = _balanced_entry(
        lines=[
            {"account_code": "1200", "debit": "5000000.00"},
            {"account_code": "1100", "credit": "4000000.00"},
        ]
    )
    resp = client.post("/api/journal-entries", json=unbalanced)
    assert resp.status_code == 422


def test_line_with_both_debit_and_credit_is_rejected(client: TestClient) -> None:
    _seed_accounts(client)
    bad = _balanced_entry(lines=[{"account_code": "1200", "debit": "100", "credit": "100"}])
    resp = client.post("/api/journal-entries", json=bad)
    assert resp.status_code == 422


def test_unknown_account_code_is_rejected(client: TestClient) -> None:
    _seed_accounts(client)
    bad = _balanced_entry(lines=[{"account_code": "9999", "debit": "100"}, {"account_code": "1100", "credit": "100"}])
    resp = client.post("/api/journal-entries", json=bad)
    assert resp.status_code == 400


def test_resend_identical_entry_returns_200_same_id(client: TestClient) -> None:
    _seed_accounts(client)
    first = client.post("/api/journal-entries", json=_balanced_entry())
    second = client.post("/api/journal-entries", json=_balanced_entry())
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]


def test_same_reference_different_lines_returns_409(client: TestClient) -> None:
    _seed_accounts(client)
    client.post("/api/journal-entries", json=_balanced_entry())
    conflicting = _balanced_entry(
        lines=[
            {"account_code": "1200", "debit": "4000000.00"},
            {"account_code": "1100", "credit": "4000000.00"},
        ]
    )
    resp = client.post("/api/journal-entries", json=conflicting)
    assert resp.status_code == 409


def test_get_by_external_reference(client: TestClient) -> None:
    _seed_accounts(client)
    client.post("/api/journal-entries", json=_balanced_entry())
    resp = client.get("/api/journal-entries/JE-1")
    assert resp.status_code == 200
    assert resp.json()["external_reference"] == "JE-1"


def test_get_unknown_reference_returns_404(client: TestClient) -> None:
    resp = client.get("/api/journal-entries/DOES-NOT-EXIST")
    assert resp.status_code == 404
