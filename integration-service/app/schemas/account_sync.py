from pydantic import BaseModel


class AccountSyncRequest(BaseModel):
    """Optional: sync just one account by code. Omitted -> sync every account
    currently in the ERP's chart of accounts. Safe to call repeatedly either
    way -- each account's sync is itself idempotent on that account's own
    external_reference (reusing the exact same GL_ACCOUNT_SYNC idempotency
    records Part A's Fineract side already enforces uniqueness for)."""

    account_code: str | None = None


class AccountSyncResult(BaseModel):
    code: str
    external_reference: str
    status: str  # "SYNCED" | "FAILED"
    fineract_gl_account_id: int | None = None
    error: str | None = None


class AccountSyncResponse(BaseModel):
    results: list[AccountSyncResult]
