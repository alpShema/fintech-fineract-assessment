from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class LoanApplicationCreate(BaseModel):
    external_reference: str
    client_uid: str
    loan_reference: str
    principal: Decimal
    expected_disbursement_date: date
    number_of_repayments: int = 12
    interest_rate_per_period: Decimal = Decimal("18")


class LoanRead(BaseModel):
    loan_reference: str
    status: str
    principal_outstanding: Decimal | None = None
    total_outstanding: Decimal | None = None


class LoanDisburseRequest(BaseModel):
    external_reference: str
    actual_disbursement_date: date


class LoanRepaymentRequest(BaseModel):
    external_reference: str
    transaction_date: date
    transaction_amount: Decimal
