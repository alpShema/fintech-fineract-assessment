from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.account import AccountType


class AccountCreate(BaseModel):
    code: str
    name: str
    type: AccountType
    currency: str
    external_reference: str

    @field_validator("currency")
    @classmethod
    def currency_is_iso_length(cls, v: str) -> str:
        if len(v) != 3:
            raise ValueError("currency must be a 3-letter ISO code (e.g. RWF)")
        return v.upper()


class AccountRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    type: AccountType
    currency: str
    external_reference: str
    created_at: datetime


class AccountBalance(BaseModel):
    code: str
    currency: str
    balance: Decimal
