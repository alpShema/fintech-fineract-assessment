from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ClientMapping(Base):
    """The local mapping table Part C requires: callers only ever use
    `client_uid`; this is the only place that business id is translated to
    Fineract's internal numeric client id. Nothing outside this table (and the
    code that reads it) should ever need to know a Fineract client id exists."""

    __tablename__ = "client_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_uid: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    fineract_client_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LoanMapping(Base):
    """Same role as ClientMapping, for `loan_reference` -> Fineract's internal
    numeric loan id. `client_uid` is stored too (not just derivable via a join)
    so a loan's owning client is available without a second lookup."""

    __tablename__ = "loan_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    loan_reference: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    fineract_loan_id: Mapped[int] = mapped_column(Integer, nullable=False)
    client_uid: Mapped[str] = mapped_column(ForeignKey("client_mappings.client_uid"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
