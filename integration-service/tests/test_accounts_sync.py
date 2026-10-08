import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import settings
from tests.conftest import API_KEY_HEADERS

ERP_ACCOUNTS = [
    {"code": "1100", "name": "Bank", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1100"},
    {"code": "1200", "name": "Loan Receivable", "type": "ASSET", "currency": "RWF", "external_reference": "GL-1200"},
]


@respx.mock
def test_sync_all_accounts(client: TestClient) -> None:
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS))
    fineract_route = respx.post(f"{settings.fineract_base_url}/glaccounts").mock(
        side_effect=lambda request: Response(200, json={"resourceId": 1})
    )

    resp = client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    assert all(r["status"] == "SYNCED" for r in results)
    assert fineract_route.call_count == 2


@respx.mock
def test_resync_does_not_call_fineract_again(client: TestClient) -> None:
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS))
    fineract_route = respx.post(f"{settings.fineract_base_url}/glaccounts").mock(
        return_value=Response(200, json={"resourceId": 1})
    )

    client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    # "Sending the same account 4 times must yield one ERP account, one
    # Fineract GL account, one mapping" -- here, syncing the whole chart
    # twice still only calls Fineract once per account.
    assert fineract_route.call_count == 2


@respx.mock
def test_sync_single_account_by_code(client: TestClient) -> None:
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS))
    respx.post(f"{settings.fineract_base_url}/glaccounts").mock(return_value=Response(200, json={"resourceId": 1}))

    resp = client.post("/api/accounts/sync", json={"account_code": "1100"}, headers=API_KEY_HEADERS)
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["code"] == "1100"


@respx.mock
def test_fineract_failure_marks_account_failed_not_an_exception(client: TestClient) -> None:
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS[:1]))
    respx.post(f"{settings.fineract_base_url}/glaccounts").mock(return_value=Response(500, json={"message": "boom"}))

    resp = client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    assert resp.status_code == 200  # the sync call itself succeeds...
    results = resp.json()["results"]
    assert results[0]["status"] == "FAILED"  # ...reporting the per-account failure, not raising
    assert results[0]["error"] is not None


@respx.mock
def test_resync_of_previously_failed_account_retries_and_still_reports_error(client: TestClient) -> None:
    """Part A/G's brief: GL sync must retry if Fineract is down, not get
    permanently stuck replaying its first failure. A second sync after a
    FAILED first attempt calls Fineract again (not a cached replay); if it
    fails again, the error detail must still surface, not silently drop."""
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS[:1]))
    fineract_route = respx.post(f"{settings.fineract_base_url}/glaccounts").mock(
        return_value=Response(500, json={"message": "boom"})
    )

    client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    second = client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)

    results = second.json()["results"]
    assert results[0]["status"] == "FAILED"
    assert results[0]["error"] is not None
    assert results[0]["fineract_gl_account_id"] is None
    assert fineract_route.call_count == 2  # retried, not replayed from cache


@respx.mock
def test_resync_of_previously_failed_account_succeeds_once_fineract_recovers(client: TestClient) -> None:
    """The other half of the retry requirement: once Fineract is back up, the
    next sync of a previously-FAILED account must actually succeed (SYNCED),
    not stay permanently stuck reporting the old failure."""
    respx.get(f"{settings.mock_erp_base_url}/api/accounts").mock(return_value=Response(200, json=ERP_ACCOUNTS[:1]))
    fineract_route = respx.post(f"{settings.fineract_base_url}/glaccounts").mock(
        side_effect=[Response(500, json={"message": "boom"}), Response(200, json={"resourceId": 9})]
    )

    first = client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    assert first.json()["results"][0]["status"] == "FAILED"

    second = client.post("/api/accounts/sync", json={}, headers=API_KEY_HEADERS)
    results = second.json()["results"]
    assert results[0]["status"] == "SYNCED"
    assert results[0]["fineract_gl_account_id"] == 9
    assert results[0]["error"] is None
    assert fineract_route.call_count == 2
