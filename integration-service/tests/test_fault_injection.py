import pytest

from app import fault_injection


@pytest.fixture(autouse=True)
def _clear_faults():
    fault_injection.clear()
    yield
    fault_injection.clear()


def test_unarmed_target_peeks_none() -> None:
    assert fault_injection.peek("fineract_disburse") is None


def test_arm_and_peek() -> None:
    fault_injection.arm("fineract_disburse", fault_injection.MODE_DOWN)
    assert fault_injection.peek("fineract_disburse") == fault_injection.MODE_DOWN


def test_consume_decrements_and_auto_clears_after_times() -> None:
    fault_injection.arm("fineract_disburse", fault_injection.MODE_DOWN, times=2)
    fault_injection.consume("fineract_disburse")
    assert fault_injection.peek("fineract_disburse") == fault_injection.MODE_DOWN  # one trigger left
    fault_injection.consume("fineract_disburse")
    assert fault_injection.peek("fineract_disburse") is None  # auto-cleared


def test_clear_single_target_leaves_others_armed() -> None:
    fault_injection.arm("fineract_disburse", fault_injection.MODE_DOWN)
    fault_injection.arm("erp_journal_entry", fault_injection.MODE_TIMEOUT_AFTER_SEND)
    fault_injection.clear("fineract_disburse")
    assert fault_injection.peek("fineract_disburse") is None
    assert fault_injection.peek("erp_journal_entry") == fault_injection.MODE_TIMEOUT_AFTER_SEND


def test_arm_rejects_unknown_target() -> None:
    with pytest.raises(ValueError, match="target"):
        fault_injection.arm("not_a_real_target", fault_injection.MODE_DOWN)


def test_arm_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        fault_injection.arm("fineract_disburse", "not_a_real_mode")


def test_status_reports_armed_faults() -> None:
    fault_injection.arm("fineract_disburse", fault_injection.MODE_DOWN, times=3)
    assert fault_injection.status() == {"fineract_disburse": {"mode": "down", "remaining": 3}}
