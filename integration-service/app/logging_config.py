import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    """Structured JSON logs. Fields match the brief's observability
    requirement exactly: correlation_id, external_reference, client_uid,
    loan_reference, fineract_transaction_id, erp_transaction_id, status,
    timestamp -- so "what happened to <external_reference>?" is answerable
    from logs plus one API call (see DECISIONS.md)."""

    FIELDS = (
        "correlation_id",
        "external_reference",
        "client_uid",
        "loan_reference",
        "fineract_transaction_id",
        "erp_transaction_id",
        "status",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        for field in self.FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
