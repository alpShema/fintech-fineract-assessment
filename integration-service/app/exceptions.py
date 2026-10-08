class ConflictError(Exception):
    """Maps to HTTP 409."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(Exception):
    """Maps to HTTP 404."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class UpstreamError(Exception):
    """A dependency (Fineract or the Mock ERP) returned an error we can't
    recover from inline. Maps to HTTP 502 -- distinct from a 4xx, since the
    problem isn't with the caller's request to *this* service."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
