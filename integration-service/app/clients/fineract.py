"""Thin wrapper around Fineract's REST API. Every method here maps to exactly
one Fineract endpoint, authenticated as the least-privilege `integration-service`
API user created in Part B (CREATE/READ_GLACCOUNT, CREATE/READ/ACTIVATE_CLIENT,
CREATE/READ_LOAN, APPROVE/DISBURSE/REPAYMENT_LOAN -- nothing else, enforced by
Fineract itself, not just by this client only calling a subset of what's
possible)."""

from typing import Any

import httpx

from app.config import settings


class FineractError(Exception):
    """Wraps a non-2xx Fineract response so callers get the real status code
    and body, not just a generic httpx exception."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Fineract returned {status_code}: {body}")


class FineractClient:
    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=settings.fineract_base_url,
            auth=(settings.fineract_username, settings.fineract_password),
            headers={"Fineract-Platform-TenantId": settings.fineract_tenant_id},
            verify=settings.fineract_verify_ssl,
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise FineractError(response.status_code, body)
        if not response.content:
            return {}
        result: dict[str, Any] = response.json()
        return result

    def create_gl_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/glaccounts", json=payload)

    def create_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/clients", json=payload)

    def get_client_by_external_id(self, client_uid: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/clients/external-id/{client_uid}")
        except FineractError as exc:
            if exc.status_code == 404:
                return None
            raise

    def create_loan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/loans", json=payload)

    def get_loan(self, fineract_loan_id: int) -> dict[str, Any]:
        return self._request("GET", f"/loans/{fineract_loan_id}")

    def approve_loan(self, fineract_loan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/loans/{fineract_loan_id}", params={"command": "approve"}, json=payload)

    def disburse_loan(self, fineract_loan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/loans/{fineract_loan_id}", params={"command": "disburse"}, json=payload)

    def repay_loan(self, fineract_loan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST", f"/loans/{fineract_loan_id}/transactions", params={"command": "repayment"}, json=payload
        )
