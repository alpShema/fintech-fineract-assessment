"""Part I: duplicates and concurrency. A standalone script, NOT collected by
pytest -- deliberately outside tests/ so a bare `pytest` run never accidentally
fires real concurrent HTTP traffic at a live Docker stack. Run explicitly:

    python concurrency_tests/test_concurrent_disbursement.py

Requires the full stack up (docker compose up -d) with a fresh-ish Fineract
loan product balance (uses real money movement against the real containers).
Evidence for I1/I2/I3 is the printed output of this script -- save it to
evidence/part-i-concurrency.log.

Why a script and not pytest: I2/I3 need true OS-level concurrency against a
real Postgres (SQLite's in-memory unit-test DB can't meaningfully exercise a
unique-constraint race -- there's nothing to race against with one
connection), and the evidence itself (Fineract's real transaction count,
Mock ERP's real journal entry count) only exists against the real stack.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

import httpx

INTEGRATION_BASE = "http://localhost:8002"
ERP_BASE = "http://localhost:8001"
FINERACT_BASE = "https://localhost:8443/fineract-provider/api/v1"
API_KEY = "integration-service-partner-key"
FINERACT_AUTH = ("mifos", "password")
TODAY = date.today().isoformat()

_fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        _fails.append(label)


def integration_client() -> httpx.Client:
    return httpx.Client(base_url=INTEGRATION_BASE, headers={"X-Api-Key": API_KEY}, timeout=30.0)


def fineract_client() -> httpx.Client:
    return httpx.Client(
        base_url=FINERACT_BASE,
        auth=FINERACT_AUTH,
        headers={"Fineract-Platform-TenantId": "default"},
        verify=False,
        timeout=30.0,
    )


def erp_client() -> httpx.Client:
    return httpx.Client(base_url=ERP_BASE, timeout=30.0)


def new_client_and_loan(client: httpx.Client, label: str, principal: str = "1000000") -> tuple[str, str]:
    ts = str(int(time.time() * 1000))
    client_uid = f"SME-{label}-{ts}"
    loan_reference = f"LN-{label}-{ts}"
    client.post(
        "/api/clients",
        json={
            "external_reference": f"REF-{label}-CLIENT-{ts}",
            "client_uid": client_uid,
            "firstname": label,
            "lastname": "Concurrency",
        },
    ).raise_for_status()
    client.post(
        "/api/loans",
        json={
            "external_reference": f"REF-{label}-LOAN-{ts}",
            "client_uid": client_uid,
            "loan_reference": loan_reference,
            "principal": principal,
            "expected_disbursement_date": TODAY,
        },
    ).raise_for_status()
    return client_uid, loan_reference


def fineract_loan_transactions(fc: httpx.Client, loan_reference: str) -> list[dict]:
    loan = fc.get(f"/loans/external-id/{loan_reference}", params={"associations": "transactions"}).json()
    return loan.get("transactions", [])


def erp_entries_for_references(ec: httpx.Client, references: set[str]) -> list[dict]:
    entries = ec.get("/api/journal-entries", params={"entry_date_from": TODAY, "entry_date_to": TODAY}).json()
    return [e for e in entries if e["external_reference"] in references]


def i1_sequential_duplicates() -> None:
    print("\n=== I1: sequential duplicates -- send the same disbursement 4 times ===")
    with integration_client() as ic, fineract_client() as fc:
        _, loan_reference = new_client_and_loan(ic, "I1")
        external_reference = f"REF-I1-DISB-{int(time.time() * 1000)}"
        payload = {"external_reference": external_reference, "actual_disbursement_date": TODAY}

        statuses = []
        for i in range(4):
            resp = ic.post(f"/api/loans/{loan_reference}/disburse", json=payload)
            statuses.append(resp.status_code)
            print(f"  request {i + 1}/4 -> HTTP {resp.status_code}")

        print(f"Requests received: {len(statuses)}")
        check("requests received == 4", len(statuses) == 4)
        check("exactly one 201 (real creation), rest replays", statuses.count(201) == 1 and statuses.count(200) == 3)

        transactions = fineract_loan_transactions(fc, loan_reference)
        disbursements = [t for t in transactions if t["type"]["code"] == "loanTransactionType.disbursement"]
        print(f"Fineract disbursements: {len(disbursements)}")
        check("Fineract disbursements == 1", len(disbursements) == 1)

    with erp_client() as ec:
        entries = erp_entries_for_references(ec, {external_reference})
        print(f"ERP journal entries: {len(entries)}")
        check("ERP journal entries == 1", len(entries) == 1)

    with integration_client() as ic:
        txn = ic.get(f"/api/transactions/{external_reference}").json()
        print(f"Integration transaction status: {txn.get('status')}")
        check("Integration transactions == 1 (COMPLETED)", txn.get("status") == "COMPLETED")


def i2_concurrent_disbursements(same_reference: bool, n: int = 10) -> None:
    label = "I2-SAME-REF" if same_reference else "I2-DIFF-REF"
    print(f"\n=== I2: {n} concurrent disbursements, {'SAME' if same_reference else 'DIFFERENT'} reference(s) ===")
    with integration_client() as ic:
        _, loan_reference = new_client_and_loan(ic, label.replace("-", ""))

    shared_reference = f"REF-{label}-{int(time.time() * 1000)}"
    references = (
        [shared_reference] * n
        if same_reference
        else [f"REF-{label}-{int(time.time() * 1000)}-{i}" for i in range(n)]
    )

    def fire(ref: str) -> int:
        with integration_client() as ic:
            resp = ic.post(
                f"/api/loans/{loan_reference}/disburse",
                json={"external_reference": ref, "actual_disbursement_date": TODAY},
            )
            return resp.status_code

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(fire, ref) for ref in references]
        statuses = [f.result() for f in as_completed(futures)]

    print(f"  {n} concurrent requests fired, statuses: {sorted(statuses)}")
    check("no 500s (every request got a clean response)", all(s < 500 for s in statuses))
    check("exactly one 201 among the batch", statuses.count(201) == 1)

    with fineract_client() as fc:
        transactions = fineract_loan_transactions(fc, loan_reference)
        disbursements = [t for t in transactions if t["type"]["code"] == "loanTransactionType.disbursement"]
        print(f"Fineract disbursements on this loan: {len(disbursements)}")
        check("exactly one Fineract disbursement", len(disbursements) == 1)

    with erp_client() as ec:
        entries = erp_entries_for_references(ec, set(references))
        print(f"ERP journal entries for this batch's references: {len(entries)}")
        check("exactly one ERP journal entry", len(entries) == 1)


def i3_concurrent_repayments(n: int = 10) -> None:
    print(f"\n=== I3: {n} concurrent repayments, different references (+ duplicates of one) ===")
    with integration_client() as ic, fineract_client() as fc:
        _, loan_reference = new_client_and_loan(ic, "I3", principal="2000000")
        disb_ref = f"REF-I3-DISB-{int(time.time() * 1000)}"
        ic.post(
            f"/api/loans/{loan_reference}/disburse",
            json={"external_reference": disb_ref, "actual_disbursement_date": TODAY},
        ).raise_for_status()

        loan_before = fc.get(f"/loans/external-id/{loan_reference}").json()
        initial_outstanding = float(loan_before["summary"]["totalOutstanding"])
        print(f"  initial outstanding (post-disbursement): {initial_outstanding}")

    ts = int(time.time() * 1000)
    distinct_refs = [f"REF-I3-REPAY-{ts}-{i}" for i in range(n - 2)]
    duplicate_ref = f"REF-I3-REPAY-{ts}-DUP"
    all_refs = distinct_refs + [duplicate_ref, duplicate_ref]  # the duplicate is sent twice, concurrently
    amount = "50000"

    def fire(ref: str) -> int:
        with integration_client() as ic:
            resp = ic.post(
                f"/api/loans/{loan_reference}/repayments",
                json={"external_reference": ref, "transaction_date": TODAY, "transaction_amount": amount},
            )
            return resp.status_code

    with ThreadPoolExecutor(max_workers=len(all_refs)) as pool:
        futures = [pool.submit(fire, ref) for ref in all_refs]
        statuses = [f.result() for f in as_completed(futures)]

    print(
        f"  {len(all_refs)} concurrent requests ({len(distinct_refs)} distinct + 1 duplicate sent twice), "
        f"statuses: {sorted(statuses)}"
    )
    check("no 500s", all(s < 500 for s in statuses))
    # Under real concurrent load, Mock ERP's own journal-entries endpoint can
    # itself contend (connection pool, DB write locks) -- a request whose
    # Fineract repayment genuinely succeeded can still land on
    # NEEDS_RECONCILIATION (202) if ITS OWN ERP-posting leg loses that race.
    # That isn't data loss or a duplicate -- it's exactly the state Part J's
    # recovery sweep exists to clean up, so run it before checking final
    # invariants (same as a real operator would after a noisy batch).
    with integration_client() as ic:
        recover_resp = ic.post("/api/admin/recover")
        print(f"  post-batch recovery sweep: {recover_resp.json()}")

    with fineract_client() as fc:
        transactions = fineract_loan_transactions(fc, loan_reference)
        repayments = [t for t in transactions if t["type"]["code"] == "loanTransactionType.repayment"]
        repayment_refs = {t.get("externalId") for t in repayments}
        print(f"Fineract repayment transactions on this loan: {len(repayments)}, distinct externalIds: {len(repayment_refs)}")
        check(
            "duplicate reference applied exactly once at Fineract",
            repayment_refs.issuperset(set(distinct_refs) | {duplicate_ref}),
        )
        check(
            "no extra repayment transactions beyond the unique references sent",
            len(repayments) == len(distinct_refs) + 1,
        )

    with erp_client() as ec:
        entries = erp_entries_for_references(ec, set(distinct_refs) | {duplicate_ref})
        print(f"ERP journal entries for this batch's references: {len(entries)} (expected {len(distinct_refs) + 1})")
        check("one ERP journal entry per distinct reference (incl. the deduped pair)", len(entries) == len(distinct_refs) + 1)

    with fineract_client() as fc:
        loan_after = fc.get(f"/loans/external-id/{loan_reference}").json()
        final_outstanding = float(loan_after["summary"]["totalOutstanding"])
        # totalOutstanding is principal + interest still owed -- a repayment's
        # full `amount` (principalPortion + interestPortion) reduces it, not
        # just the principal share.
        total_amount_applied = sum(float(t["amount"]) for t in repayments)
        expected_outstanding = initial_outstanding - total_amount_applied
        print(
            f"  final outstanding: {final_outstanding}, "
            f"expected (initial - sum of Fineract's own recorded amounts): {expected_outstanding}"
        )
        check(
            "final outstanding matches Fineract's own recorded breakdown",
            abs(final_outstanding - expected_outstanding) < 0.01,
        )


if __name__ == "__main__":
    i1_sequential_duplicates()
    i2_concurrent_disbursements(same_reference=True)
    i2_concurrent_disbursements(same_reference=False)
    i3_concurrent_repayments()

    print("\n" + "=" * 70)
    if _fails:
        print(f"FAILED: {len(_fails)} check(s) did not pass:")
        for f in _fails:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PART I CHECKS PASSED")
