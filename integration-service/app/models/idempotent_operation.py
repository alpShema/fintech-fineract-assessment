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
    """Part J models the lifecycle as an explicit state machine. The brief's
    example is RECEIVED -> FINERACT_PENDING -> FINERACT_DONE -> ERP_PENDING ->
    COMPLETED, plus FAILED and NEEDS_RECONCILIATION. RECEIVED and
    FINERACT_PENDING collapse into the single IN_PROGRESS value below -- there
    is no real work done between "a request arrived" and "we're about to call
    Fineract" in this design, so persisting them as two separate states would
    just be two rows that always transition together. (This also means
    IN_PROGRESS/COMPLETED/FAILED, used by every operation type since Part F,
    didn't need to be renamed -- GL_ACCOUNT_SYNC/CLIENT_CREATE/
    LOAN_APPLICATION only ever touch Fineract and never need the three new
    values below; only LOAN_DISBURSEMENT/LOAN_REPAYMENT's two-leg flow does.)

    Allowed transitions for LOAN_DISBURSEMENT/LOAN_REPAYMENT:
      IN_PROGRESS -> FINERACT_DONE      (Fineract call succeeded)
      IN_PROGRESS -> FAILED             (Fineract rejected it outright -- a
                                         real 4xx/domain-rule violation, not a
                                         timeout; safe to retry with a new
                                         external_reference)
      IN_PROGRESS -> IN_PROGRESS        (a timeout/crash with Fineract's own
                                         outcome still unknown -- see
                                         app/recovery.py; stays IN_PROGRESS
                                         until either the real outcome is
                                         found or a grace period elapses)
      FINERACT_DONE -> ERP_PENDING      (about to post the journal entry)
      ERP_PENDING -> COMPLETED          (ERP accepted the entry)
      ERP_PENDING -> NEEDS_RECONCILIATION (ERP failed/timed out -- Fineract's
                                         money movement already happened, so
                                         this is NOT a clean "nothing
                                         happened" failure; it needs recovery,
                                         not a fresh retry from scratch)
      NEEDS_RECONCILIATION -> COMPLETED (the recovery sweep's retried ERP leg
                                         finally succeeded)
    """

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    FINERACT_DONE = "FINERACT_DONE"
    ERP_PENDING = "ERP_PENDING"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


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

    # Populated only for LOAN_DISBURSEMENT/LOAN_REPAYMENT -- the two-leg
    # (Fineract then ERP) operations Part J's recovery machinery needs to
    # find and act on without re-deriving the loan from request_payload.
    # loan_reference/fineract_loan_id are denormalized (also derivable via
    # LoanMapping) purely so the recovery sweep doesn't need a join per row.
    loan_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    fineract_loan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Set the moment Fineract confirms success -- the durable fact a crash
    # between the Fineract leg and the ERP leg (Part J scenario 5) or a
    # stale-row recovery sweep keys off, so Fineract is never called twice.
    fineract_transaction_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
