import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AccountType(str, enum.Enum):
    """Mirrors Fineract's own GL account classification (ASSET/LIABILITY/EQUITY/
    INCOME/EXPENSE) -- this is what the Integration Service's account-type mapping
    (Part A's Python side, "map ERP account types to Fineract GL types explicitly,
    in one tested place") translates, not reinvents."""

    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    INCOME = "INCOME"
    EXPENSE = "EXPENSE"

    @property
    def normal_balance_is_debit(self) -> bool:
        """Asset/expense accounts increase on the debit side; liability/equity/income
        accounts increase on the credit side. Used by the balance endpoint to report
        a signed balance that matches real accounting convention, not just a raw
        debit-minus-credit number regardless of account type."""
        return self in (AccountType.ASSET, AccountType.EXPENSE)


class Account(Base):
    """One row per chart-of-accounts entry. `code` and `external_reference` are
    both unique on purpose and serve different roles: `code` is the ERP's own
    human-facing account number (e.g. "1200"); `external_reference` is the
    idempotency key for the create request itself (e.g. "GL-1200") and is also
    the same value the Integration Service later uses as Fineract's
    external_reference when syncing this account into Fineract's GL -- one
    business key, used consistently end to end."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[AccountType] = mapped_column(Enum(AccountType, name="account_type"), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
