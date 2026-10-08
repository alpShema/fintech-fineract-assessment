"""Part H: journal entries are derived from Fineract's own transaction data
(the authoritative principal/interest split Fineract already computed via its
amortization schedule), never recalculated here. See DECISIONS.md for where
this split comes from and how overpayments/reversals would be handled.

Both functions take a Fineract loan-transaction dict exactly as returned by
`GET /loans/{id}?associations=transactions` (or the per-transaction `GET
/loans/{id}/transactions/{id}`) -- same shape either way.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

BANK_ACCOUNT_CODE = "1100"
LOAN_RECEIVABLE_ACCOUNT_CODE = "1200"
INTEREST_INCOME_ACCOUNT_CODE = "4000"


@dataclass
class JournalLine:
    account_code: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")


def _decimal(transaction: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(transaction.get(key, 0)))


def disbursement_journal_lines(transaction: dict[str, Any]) -> list[JournalLine]:
    """Disbursement: DR Loan Receivable / CR Bank, for the full amount --
    Fineract doesn't split a disbursement into principal/interest (that only
    happens on repayment), so `amount` is the whole of it."""
    amount = _decimal(transaction, "amount")
    return [
        JournalLine(account_code=LOAN_RECEIVABLE_ACCOUNT_CODE, debit=amount),
        JournalLine(account_code=BANK_ACCOUNT_CODE, credit=amount),
    ]


def repayment_journal_lines(transaction: dict[str, Any]) -> list[JournalLine]:
    """Repayment: DR Bank (full amount received) / CR Loan Receivable
    (principal portion) / CR Interest Income (interest portion).

    Fee and penalty portions, if a loan product has charges configured, are
    folded into the Interest Income line -- there's no dedicated fee/penalty
    GL account in this system's chart of accounts, and erroring on an
    otherwise-ordinary repayment just because a product has fees would be
    wrong (fees are a normal case, not an edge case). Overpayment is
    genuinely undefined here -- it needs a suspense/liability account this
    system doesn't have -- so that one raises rather than silently misposts.
    """
    amount = _decimal(transaction, "amount")
    principal = _decimal(transaction, "principalPortion")
    interest = _decimal(transaction, "interestPortion")
    fees = _decimal(transaction, "feeChargesPortion") + _decimal(transaction, "penaltyChargesPortion")
    overpayment = _decimal(transaction, "overpaymentPortion")

    if overpayment > 0:
        raise ValueError(
            f"repayment transaction has a nonzero overpaymentPortion ({overpayment}) -- "
            "this mapping has no suspense/liability account to post an overpayment against; "
            "see DECISIONS.md for how this would need to be handled"
        )

    interest_income = interest + fees

    # A repayment can legitimately be all-principal (interest already fully
    # paid for the period) or all-interest (principal already fully repaid,
    # e.g. a final true-up) -- a zero-amount line isn't a real line, and
    # Mock ERP's schema correctly rejects one (found live, under Part I's
    # concurrent-repayment test: several of many rapid repayments on one
    # loan ended up with interestPortion == 0, and always emitting a
    # credit=0 Interest Income line got every one of them a 422). Omit
    # whichever side is genuinely zero rather than posting a fake line.
    lines = [JournalLine(account_code=BANK_ACCOUNT_CODE, debit=amount)]
    if principal > 0:
        lines.append(JournalLine(account_code=LOAN_RECEIVABLE_ACCOUNT_CODE, credit=principal))
    if interest_income > 0:
        lines.append(JournalLine(account_code=INTEREST_INCOME_ACCOUNT_CODE, credit=interest_income))
    if len(lines) < 2:
        raise ValueError(
            f"repayment transaction (amount={amount}) has neither a principal nor an interest/fee "
            "portion to credit -- nothing to balance the Bank debit against"
        )
    return lines
