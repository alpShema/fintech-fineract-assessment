from typing import Any

from pydantic import BaseModel


class TransactionRead(BaseModel):
    external_reference: str
    operation_type: str
    status: str
    result: dict[str, Any] | None = None
