import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import settings
from tests.conftest import API_KEY_HEADERS


def _payload(**overrides: object) -> dict:
    base = {"external_reference": "REF-C1", "client_uid": "SME-000001", "firstname": "Amina", "lastname": "Uwase"}
    base.update(overrides)
    return base


@respx.mock
def test_create_client_returns_201(client: TestClient) -> None:
    respx.post(f"{settings.fineract_base_url}/clients").mock(return_value=Response(200, json={"resourceId": 1}))
    resp = client.post("/api/clients", json=_payload(), headers=API_KEY_HEADERS)
    assert resp.status_code == 201
    assert resp.json()["client_uid"] == "SME-000001"


@respx.mock
def test_resend_identical_payload_returns_200_no_second_fineract_call(client: TestClient) -> None:
    route = respx.post(f"{settings.fineract_base_url}/clients").mock(return_value=Response(200, json={"resourceId": 1}))
    first = client.post("/api/clients", json=_payload(), headers=API_KEY_HEADERS)
    assert first.status_code == 201

    second = client.post("/api/clients", json=_payload(), headers=API_KEY_HEADERS)
    assert second.status_code == 200
    assert second.json() == first.json()
    assert route.call_count == 1  # Fineract called exactly once, not twice


@respx.mock
def test_same_reference_different_payload_returns_409(client: TestClient) -> None:
    respx.post(f"{settings.fineract_base_url}/clients").mock(return_value=Response(200, json={"resourceId": 1}))
    client.post("/api/clients", json=_payload(), headers=API_KEY_HEADERS)

    resp = client.post("/api/clients", json=_payload(firstname="Different"), headers=API_KEY_HEADERS)
    assert resp.status_code == 409


@respx.mock
def test_different_reference_same_client_uid_returns_409_without_calling_fineract(client: TestClient) -> None:
    route = respx.post(f"{settings.fineract_base_url}/clients").mock(return_value=Response(200, json={"resourceId": 1}))
    client.post("/api/clients", json=_payload(), headers=API_KEY_HEADERS)

    resp = client.post("/api/clients", json=_payload(external_reference="REF-C2"), headers=API_KEY_HEADERS)
    assert resp.status_code == 409
    assert route.call_count == 1  # second call never reached Fineract -- rejected before that


@respx.mock
def test_get_client_by_client_uid(client: TestClient) -> None:
    respx.get(f"{settings.fineract_base_url}/clients/external-id/SME-000001").mock(
        return_value=Response(200, json={"status": {"value": "Active"}, "displayName": "Amina Uwase"})
    )
    resp = client.get("/api/clients/SME-000001", headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"client_uid": "SME-000001", "status": "Active", "display_name": "Amina Uwase"}


@respx.mock
def test_get_unknown_client_returns_404(client: TestClient) -> None:
    respx.get(f"{settings.fineract_base_url}/clients/external-id/DOES-NOT-EXIST").mock(return_value=Response(404, json={}))
    resp = client.get("/api/clients/DOES-NOT-EXIST", headers=API_KEY_HEADERS)
    assert resp.status_code == 404


def test_missing_api_key_returns_401(client: TestClient) -> None:
    resp = client.get("/api/clients/SME-000001")
    assert resp.status_code == 401
