from typing import Any

import httpx

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
