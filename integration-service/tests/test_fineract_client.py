import respx
from httpx import Response
from pytest import MonkeyPatch

from app.clients import fineract as fineract_module
from app.clients.fineract import FineractClient, FineractError
from app.config import settings


def _skip_backoff_sleep(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(fineract_module.time, "sleep", lambda _seconds: None)

_CONFLICT_BODY = {
    "developerMessage": "The server is currently unable to handle the request due to concurrent modification",
    "httpStatusCode": "409",
    "defaultUserMessage": "The server is currently unable to handle the request due to concurrent modification, please try again",
    "userMessageGlobalisationCode": "error.msg.platform.service.query.conflict",
}


@respx.mock
def test_retries_on_fineracts_own_optimistic_lock_conflict(monkeypatch: MonkeyPatch) -> None:
    """Part I: concurrent repayments on the same loan hit Fineract's own
    Hibernate optimistic-lock conflict (409, 'please try again' -- confirmed
    live). The client must retry this specific conflict, not surface it as a
    generic failure on the first attempt."""
    _skip_backoff_sleep(monkeypatch)
    route = respx.post(f"{settings.fineract_base_url}/loans/3/transactions", params={"command": "repayment"}).mock(
        side_effect=[
            Response(409, json=_CONFLICT_BODY),
            Response(409, json=_CONFLICT_BODY),
            Response(200, json={"resourceId": 1}),
        ]
    )
    client = FineractClient()
    try:
        result = client.repay_loan(3, {"transactionAmount": "1000"})
    finally:
        client.close()
    assert result == {"resourceId": 1}
    assert route.call_count == 3


@respx.mock
def test_gives_up_after_max_retries_on_persistent_conflict(monkeypatch: MonkeyPatch) -> None:
    _skip_backoff_sleep(monkeypatch)
    route = respx.post(f"{settings.fineract_base_url}/loans/3/transactions", params={"command": "repayment"}).mock(
        return_value=Response(409, json=_CONFLICT_BODY)
    )
    client = FineractClient()
    try:
        try:
            client.repay_loan(3, {"transactionAmount": "1000"})
            raise AssertionError("expected FineractError")
        except FineractError as exc:
            assert exc.status_code == 409
    finally:
        client.close()
    assert route.call_count == 6  # 1 initial + 5 retries, bounded, not infinite


@respx.mock
def test_other_409s_are_not_retried() -> None:
    """A 409 that ISN'T Fineract's optimistic-lock conflict (e.g. a genuine
    business-rule rejection) must fail immediately, not burn through retries
    for no reason."""
    route = respx.post(f"{settings.fineract_base_url}/loans/3/transactions", params={"command": "repayment"}).mock(
        return_value=Response(409, json={"userMessageGlobalisationCode": "some.other.conflict"})
    )
    client = FineractClient()
    try:
        try:
            client.repay_loan(3, {"transactionAmount": "1000"})
            raise AssertionError("expected FineractError")
        except FineractError as exc:
            assert exc.status_code == 409
    finally:
        client.close()
    assert route.call_count == 1
