"""Part J: "build a way to inject failures on demand." A small in-process,
admin-toggled registry -- not a proxy, not a feature-flag service, just a
dict guarded by a lock, because that's all five failure scenarios in the
brief actually need. Armed via `app/routers/admin.py` (test-only, gated
behind the same API key as every other route).

Two usable points per call site, matching the two kinds of failure the brief
actually describes:
  - "down": the downstream never gets the request at all (Scenario 2:
    Fineract down; also usable for an unreachable ERP in Scenario 1).
  - "timeout_after_send": the real request IS made -- the downstream
    genuinely processes it -- but the caller never sees the response
    (Scenarios 3/4: a timeout after Fineract/ERP already did the work).

A fault auto-clears after `times` triggers so a forgotten toggle can't bleed
into the next demo or test run.
"""

import threading
from dataclasses import dataclass

MODE_DOWN = "down"
MODE_TIMEOUT_AFTER_SEND = "timeout_after_send"
VALID_MODES = {MODE_DOWN, MODE_TIMEOUT_AFTER_SEND}

TARGET_FINERACT_DISBURSE = "fineract_disburse"
TARGET_FINERACT_REPAY = "fineract_repay"
TARGET_ERP_JOURNAL_ENTRY = "erp_journal_entry"
VALID_TARGETS = {TARGET_FINERACT_DISBURSE, TARGET_FINERACT_REPAY, TARGET_ERP_JOURNAL_ENTRY}


@dataclass
class ArmedFault:
    mode: str
    remaining: int


_lock = threading.Lock()
_armed: dict[str, ArmedFault] = {}


def arm(target: str, mode: str, times: int = 1) -> None:
    if target not in VALID_TARGETS:
        raise ValueError(f"unknown fault-injection target {target!r}, must be one of {sorted(VALID_TARGETS)}")
    if mode not in VALID_MODES:
        raise ValueError(f"unknown fault-injection mode {mode!r}, must be one of {sorted(VALID_MODES)}")
    with _lock:
        _armed[target] = ArmedFault(mode=mode, remaining=times)


def clear(target: str | None = None) -> None:
    with _lock:
        if target is None:
            _armed.clear()
        else:
            _armed.pop(target, None)


def status() -> dict[str, dict[str, int | str]]:
    with _lock:
        return {target: {"mode": fault.mode, "remaining": fault.remaining} for target, fault in _armed.items()}


def peek(target: str) -> str | None:
    """What mode (if any) is currently armed for this target -- does not
    consume it. Call this before making the real downstream request to
    decide whether to skip it entirely (mode == down) or make it and check
    again afterward (mode == timeout_after_send)."""
    with _lock:
        fault = _armed.get(target)
        return fault.mode if fault is not None else None


def consume(target: str) -> None:
    """Record that an armed fault just fired once. Call this exactly when the
    simulated failure is actually raised -- never speculatively."""
    with _lock:
        fault = _armed.get(target)
        if fault is None:
            return
        fault.remaining -= 1
        if fault.remaining <= 0:
            del _armed[target]
