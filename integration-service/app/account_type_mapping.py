"""The one place ERP account types get translated to Fineract's GL account
`type` codes. Nothing else in this service should hardcode this mapping --
if Fineract's numbering ever needs to be double-checked, this is the only
file that matters.

Fineract's codes were confirmed against org.apache.fineract.accounting.
glaccount.domain.GLAccountType (fineract-upstream, tag 1.10.0): ASSET=1,
LIABILITY=2, EQUITY=3, INCOME=4, EXPENSE=5.
"""

from enum import Enum


class ErpAccountType(str, Enum):
    """Mirrors the Mock ERP's own AccountType (app/models/account.py in
    mock-erp) -- duplicated here deliberately rather than imported, since
    the Integration Service and Mock ERP are separate deployable services
    with their own dependency trees, not a shared library."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"


_ERP_TO_FINERACT_GL_TYPE: dict[ErpAccountType, int] = {
    ErpAccountType.ASSET: 1,
    ErpAccountType.LIABILITY: 2,
    ErpAccountType.EQUITY: 3,
    ErpAccountType.INCOME: 4,
    ErpAccountType.EXPENSE: 5,
}


def map_erp_account_type_to_fineract(erp_type: ErpAccountType | str) -> int:
    key = ErpAccountType(erp_type)
    return _ERP_TO_FINERACT_GL_TYPE[key]
