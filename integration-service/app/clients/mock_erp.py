from typing import Any

import httpx

from app import fault_injection
from app.config import settings


class MockErpError(Exception):
    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Mock ERP returned {status_code}: {body}")


class MockErpClient:
    def __init__(self) -> None:
        self._client = httpx.Client(base_url=settings.mock_erp_base_url, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def list_accounts(self) -> list[dict[str, Any]]:
        response = self._client.get("/api/accounts")
        if response.status_code >= 400:
            raise MockErpError(response.status_code, response.text)
        result: list[dict[str, Any]] = response.json()
        return result

    def create_journal_entry(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = fault_injection.TARGET_ERP_JOURNAL_ENTRY
        if fault_injection.peek(target) == fault_injection.MODE_DOWN:
            fault_injection.consume(target)
            raise httpx.ConnectError(f"simulated: {target} down (fault injection)")

        response = self._client.post("/api/journal-entries", json=payload)
        if response.status_code >= 400:
            raise MockErpError(response.status_code, response.text)

        if fault_injection.peek(target) == fault_injection.MODE_TIMEOUT_AFTER_SEND:
            fault_injection.consume(target)
            raise httpx.TimeoutException(f"simulated: {target} timeout after send (fault injection)")

        result: dict[str, Any] = response.json()
        return result

    def get_journal_entry(self, external_reference: str) -> dict[str, Any] | None:
        response = self._client.get(f"/api/journal-entries/{external_reference}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise MockErpError(response.status_code, response.text)
        result: dict[str, Any] = response.json()
        return result

    def list_journal_entries(self, entry_date_from: str | None = None, entry_date_to: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if entry_date_from:
            params["entry_date_from"] = entry_date_from
        if entry_date_to:
            params["entry_date_to"] = entry_date_to
        response = self._client.get("/api/journal-entries", params=params)
        if response.status_code >= 400:
            raise MockErpError(response.status_code, response.text)
        result: list[dict[str, Any]] = response.json()
        return result
