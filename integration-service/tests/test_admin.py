import pytest
from fastapi.testclient import TestClient

from app import fault_injection
from tests.conftest import API_KEY_HEADERS


@pytest.fixture(autouse=True)
def _clear_faults():
    fault_injection.clear()
    yield
    fault_injection.clear()


def test_arm_fault_requires_api_key(client: TestClient) -> None:
    resp = client.post("/api/admin/fault-injection", json={"target": "fineract_disburse", "mode": "down"})
    assert resp.status_code == 401


def test_arm_then_status_then_clear(client: TestClient) -> None:
    resp = client.post(
        "/api/admin/fault-injection",
        json={"target": "fineract_disburse", "mode": "down", "times": 2},
        headers=API_KEY_HEADERS,
    )
    assert resp.status_code == 201
    assert resp.json()["armed"] == {"fineract_disburse": {"mode": "down", "remaining": 2}}

    status_resp = client.get("/api/admin/fault-injection", headers=API_KEY_HEADERS)
    assert status_resp.json()["armed"] == {"fineract_disburse": {"mode": "down", "remaining": 2}}

    clear_resp = client.delete("/api/admin/fault-injection", headers=API_KEY_HEADERS)
    assert clear_resp.json()["armed"] == {}


def test_arm_rejects_unknown_target(client: TestClient) -> None:
    resp = client.post(
        "/api/admin/fault-injection", json={"target": "not_real", "mode": "down"}, headers=API_KEY_HEADERS
    )
    assert resp.status_code == 400


def test_recover_with_nothing_stuck_processes_zero(client: TestClient) -> None:
    resp = client.post("/api/admin/recover", headers=API_KEY_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"processed": 0, "results": []}
