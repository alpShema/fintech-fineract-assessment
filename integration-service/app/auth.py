from fastapi import Header, HTTPException, status

from app.config import settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """Simple shared-key auth for the "financing partner" calling this
    service. Deliberately not full OAuth2/JWT -- see DECISIONS.md for why
    that wasn't warranted for this assessment's scope. A FastAPI dependency,
    not middleware, so it shows up in OpenAPI docs as a header parameter on
    every protected route.

    The header is declared *optional* here (not `Header(...)`) on purpose:
    a required Header makes FastAPI raise its own RequestValidationError
    (422) for a missing header before this function body ever runs -- which
    would make "no API key" and "wrong API key" return different status
    codes (422 vs our own 401) for what's really the same failure from the
    caller's point of view. Checking manually keeps both cases a clean 401.
    """
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key")
