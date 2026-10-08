from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, model_validator


class JournalEntryLineCreate(BaseModel):
    account_code: str
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")

    @model_validator(mode="after")
    def exactly_one_side_set(self) -> "JournalEntryLineCreate":
        if self.debit < 0 or self.credit < 0:
            raise ValueError("debit/credit amounts must not be negative")
        if (self.debit > 0) == (self.credit > 0):
            raise ValueError(
                f"line for account {self.account_code!r} must have exactly one of debit/credit > 0, not both or neither"
            )
        return self


class JournalEntryCreate(BaseModel):
    external_reference: str
    entry_date: date
    description: str | None = None
    lines: list[JournalEntryLineCreate]

    @model_validator(mode="after")
    def must_balance(self) -> "JournalEntryCreate":
        if len(self.lines) < 2:
            raise ValueError("a journal entry needs at least 2 lines (one debit, one credit)")
        total_debit = sum((line.debit for line in self.lines), Decimal("0"))
        total_credit = sum((line.credit for line in self.lines), Decimal("0"))
        if total_debit != total_credit:
            raise ValueError(f"entry does not balance: total debit {total_debit} != total credit {total_credit}")
        return self


class JournalEntryLineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    account_code: str
    debit: Decimal
    credit: Decimal


class JournalEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    external_reference: str
    entry_date: date
    description: str | None
    created_at: datetime
    lines: list[JournalEntryLineRead]
