from pydantic import BaseModel


class ErrorResponse(BaseModel):
    error_code: str
    message: str
    correlation_id: str | None = None
