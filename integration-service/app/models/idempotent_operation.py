import enum
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base

# Use JSONB on Postgres (real deployment), plain JSON elsewhere (SQLite in unit
# tests) -- SQLAlchemy's JSON type already does this automatically when given
# a .with_variant, so this isn't actually load-bearing, just explicit about it.
JsonType = JSON().with_variant(JSONB(), "postgresql")


class OperationType(str, enum.Enum):
    """Every state-changing operation Part F requires idempotency for. Each has
    its own uniqueness scope -- see IdempotentOperation's docstring."""

    GL_ACCOUNT_SYNC = "GL_ACCOUNT_SYNC"
    CLIENT_CREATE = "CLIENT_CREATE"
    LOAN_APPLICATION = "LOAN_APPLICATION"
    LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT"
    LOAN_REPAYMENT = "LOAN_REPAYMENT"


class OperationStatus(str, enum.Enum):
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class IdempotentOperation(Base):
    """One row per state-changing request, tracked by (operation_type,
    external_reference) -- this is what Part F's whole idempotency contract is
    built on:

      new reference                          -> process once
      same reference, same payload, finished  -> return the original result (200)
      same reference, same payload, running   -> 202 with current status
      same reference, different payload       -> 409

    `scope_key` is the one exception to "external_reference is the idempotency
    key": for LOAN_DISBURSEMENT specifically, the brief requires uniqueness
    *per loan*, not per external_reference -- "concurrent requests with
    different references for the same loan still result in exactly one
    Fineract disbursement." So disbursement rows also set scope_key to the
    loan_reference, and a second UNIQUE constraint on (operation_type,
    scope_key) catches a second disbursement attempt even when it arrives with
    a brand new external_reference. (NULL scope_key on every other operation
    type is exempt from that constraint under normal SQL NULL semantics --
    same pattern already used for Fineract's and the Mock ERP's nullable
    unique columns.)
    """

    __tablename__ = "idempotent_operations"
    __table_args__ = (
        UniqueConstraint("operation_type", "external_reference", name="uq_operation_external_reference"),
        UniqueConstraint("operation_type", "scope_key", name="uq_operation_scope_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_type: Mapped[OperationType] = mapped_column(Enum(OperationType, name="operation_type"), nullable=False)
    external_reference: Mapped[str] = mapped_column(String(100), nullable=False)
    scope_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    status: Mapped[OperationStatus] = mapped_column(
        Enum(OperationStatus, name="operation_status"), nullable=False, default=OperationStatus.IN_PROGRESS
    )
    response_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
