from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """Consistent error shape across every endpoint, matching the correlation-id
    convention used by the Integration Service (Part E requires "consistent error
    format, correlation ID on every request/response")."""

    error_code: str
    message: str
    correlation_id: str | None = None
