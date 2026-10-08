"""Thin wrapper around Fineract's REST API. Every method here maps to exactly
one Fineract endpoint, authenticated as the least-privilege `integration-service`
API user created in Part B (CREATE/READ_GLACCOUNT, CREATE/READ/ACTIVATE_CLIENT,
CREATE/READ_LOAN, APPROVE/DISBURSE/REPAYMENT_LOAN -- nothing else, enforced by
Fineract itself, not just by this client only calling a subset of what's
possible)."""

import time
from typing import Any

import httpx

from app import fault_injection
from app.config import settings

# Fineract's own optimistic locking on a loan entity: concurrent writes to
# the SAME loan (e.g. two repayments racing) make Hibernate's version check
# fail, and Fineract surfaces that as this specific 409 -- its own message
# literally says "please try again". Confirmed live under Part I's
# concurrent-repayment test: multiple legitimate (non-duplicate) repayments
# fired at once against one loan got this, not a business-rule rejection.
# Retried at the HTTP-client layer (bounded, short backoff) because this is
# Fineract's own concurrency control doing exactly what it's designed to do,
# not a signal that anything is wrong with the request itself.
_CONFLICT_GLOBALISATION_CODE = "error.msg.platform.service.query.conflict"
_MAX_CONFLICT_RETRIES = 5
_CONFLICT_BACKOFF_SECONDS = 0.15


class FineractError(Exception):
    """Wraps a non-2xx Fineract response so callers get the real status code
    and body, not just a generic httpx exception."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"Fineract returned {status_code}: {body}")

    @property
    def is_optimistic_lock_conflict(self) -> bool:
        return self.status_code == 409 and isinstance(self.body, dict) and (
            self.body.get("userMessageGlobalisationCode") == _CONFLICT_GLOBALISATION_CODE
        )


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
        for attempt in range(_MAX_CONFLICT_RETRIES + 1):
            response = self._client.request(method, path, **kwargs)
            if response.status_code >= 400:
                try:
                    body = response.json()
                except ValueError:
                    body = response.text
                error = FineractError(response.status_code, body)
                if error.is_optimistic_lock_conflict and attempt < _MAX_CONFLICT_RETRIES:
                    time.sleep(_CONFLICT_BACKOFF_SECONDS * (attempt + 1))
                    continue
                raise error
            if not response.content:
                return {}
            result: dict[str, Any] = response.json()
            return result
        raise AssertionError("unreachable")  # the loop always either returns or raises

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
        self._maybe_fail_before(fault_injection.TARGET_FINERACT_DISBURSE)
        result = self._request("POST", f"/loans/{fineract_loan_id}", params={"command": "disburse"}, json=payload)
        self._maybe_fail_after(fault_injection.TARGET_FINERACT_DISBURSE)
        return result

    def repay_loan(self, fineract_loan_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self._maybe_fail_before(fault_injection.TARGET_FINERACT_REPAY)
        result = self._request(
            "POST", f"/loans/{fineract_loan_id}/transactions", params={"command": "repayment"}, json=payload
        )
        self._maybe_fail_after(fault_injection.TARGET_FINERACT_REPAY)
        return result

    def list_loan_transactions(self, fineract_loan_id: int) -> list[dict[str, Any]]:
        """Every transaction on this loan (disbursements, repayments, accrual
        entries, etc.), each with its full breakdown -- `amount`,
        `principalPortion`, `interestPortion`, `externalId`,
        `manuallyReversed` -- in one call. Feeds Part H's mapping, Part J3's
        externalId lookup, and Part K's reconciliation."""
        result = self._request("GET", f"/loans/{fineract_loan_id}", params={"associations": "transactions"})
        transactions: list[dict[str, Any]] = result.get("transactions", [])
        return transactions

    def find_loan_transaction_by_external_id(self, fineract_loan_id: int, external_id: str) -> dict[str, Any] | None:
        """Part J3's recovery mechanism: after a timeout, look up whether the
        disbursement/repayment we sent (with this externalId) actually
        landed, rather than blindly retrying. Exact field match, not a
        heuristic over balance/state."""
        for transaction in self.list_loan_transactions(fineract_loan_id):
            if transaction.get("externalId") == external_id:
                return transaction
        return None

    @staticmethod
    def _maybe_fail_before(target: str) -> None:
        if fault_injection.peek(target) == fault_injection.MODE_DOWN:
            fault_injection.consume(target)
            raise httpx.ConnectError(f"simulated: {target} down (fault injection)")

    @staticmethod
    def _maybe_fail_after(target: str) -> None:
        if fault_injection.peek(target) == fault_injection.MODE_TIMEOUT_AFTER_SEND:
            fault_injection.consume(target)
            raise httpx.TimeoutException(f"simulated: {target} timeout after send (fault injection)")
