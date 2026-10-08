from decimal import Decimal

import pytest

from app.accounting_mapping import disbursement_journal_lines, repayment_journal_lines


def test_disbursement_debits_loan_receivable_credits_bank() -> None:
    transaction = {"amount": 1000000.0}
    lines = disbursement_journal_lines(transaction)
    assert lines[0].account_code == "1200" and lines[0].debit == Decimal("1000000.0")
    assert lines[1].account_code == "1100" and lines[1].credit == Decimal("1000000.0")
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit


def test_repayment_splits_principal_and_interest_from_fineracts_breakdown() -> None:
    """Real shape confirmed against a running Fineract 1.10.0 container."""
    transaction = {
        "amount": 100000.0,
        "principalPortion": 76680.0,
        "interestPortion": 23320.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
    }
    lines = repayment_journal_lines(transaction)
    assert lines[0].account_code == "1100" and lines[0].debit == Decimal("100000.0")
    assert lines[1].account_code == "1200" and lines[1].credit == Decimal("76680.0")
    assert lines[2].account_code == "4000" and lines[2].credit == Decimal("23320.0")
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit


def test_repayment_folds_fees_and_penalties_into_interest_income() -> None:
    """A loan product with charges is an ordinary case, not an error -- there's
    no dedicated fee/penalty GL account in this chart of accounts, so fees
    and penalties fold into Interest Income rather than raising."""
    transaction = {
        "amount": 110000.0,
        "principalPortion": 76680.0,
        "interestPortion": 23320.0,
        "feeChargesPortion": 5000.0,
        "penaltyChargesPortion": 5000.0,
        "overpaymentPortion": 0,
    }
    lines = repayment_journal_lines(transaction)
    assert lines[2].credit == Decimal("33320.0")  # 23320 interest + 5000 fee + 5000 penalty
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit


def test_repayment_with_overpayment_raises_value_error() -> None:
    """Overpayment needs a suspense/liability account this system doesn't
    have -- must raise, not silently mispost."""
    transaction = {
        "amount": 120000.0,
        "principalPortion": 76680.0,
        "interestPortion": 23320.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 20000.0,
    }
    with pytest.raises(ValueError, match="overpaymentPortion"):
        repayment_journal_lines(transaction)


def test_disbursement_missing_optional_fields_defaults_to_zero() -> None:
    """A transaction dict that only has 'amount' (e.g. hand-built in a test)
    shouldn't KeyError on the portions it doesn't touch."""
    lines = disbursement_journal_lines({"amount": 500})
    assert lines[0].debit == Decimal("500")


def test_repayment_with_zero_interest_omits_the_interest_line() -> None:
    """Found live under Part I's concurrent-repayment test: a repayment that
    is entirely principal (interest already paid for the period) must not
    emit a credit=0 Interest Income line -- Mock ERP's schema correctly
    rejects a zero-amount line with a 422, and every one of these repayments
    was failing that way before this fix."""
    transaction = {
        "amount": 50000.0,
        "principalPortion": 50000.0,
        "interestPortion": 0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
    }
    lines = repayment_journal_lines(transaction)
    assert len(lines) == 2
    assert {line.account_code for line in lines} == {"1100", "1200"}
    total_debit = sum((line.debit for line in lines), Decimal("0"))
    total_credit = sum((line.credit for line in lines), Decimal("0"))
    assert total_debit == total_credit


def test_repayment_with_zero_principal_omits_the_principal_line() -> None:
    """The mirror case: a final true-up repayment that's entirely interest,
    principal already fully repaid."""
    transaction = {
        "amount": 5000.0,
        "principalPortion": 0,
        "interestPortion": 5000.0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
    }
    lines = repayment_journal_lines(transaction)
    assert len(lines) == 2
    assert {line.account_code for line in lines} == {"1100", "4000"}


def test_repayment_with_nothing_to_credit_raises_value_error() -> None:
    transaction = {
        "amount": 0,
        "principalPortion": 0,
        "interestPortion": 0,
        "feeChargesPortion": 0,
        "penaltyChargesPortion": 0,
        "overpaymentPortion": 0,
    }
    with pytest.raises(ValueError, match="neither a principal nor"):
        repayment_journal_lines(transaction)
