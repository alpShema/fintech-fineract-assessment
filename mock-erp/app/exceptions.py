class ConflictError(Exception):
    """Same external_reference (or code) already exists with a different payload,
    or a unique business key collided. Maps to HTTP 409."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(Exception):
    """Maps to HTTP 404."""

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)
