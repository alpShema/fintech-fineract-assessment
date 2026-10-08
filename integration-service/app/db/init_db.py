"""No migration tool here either, same reasoning as Mock ERP's init_db.py."""

from app.db.base import Base
from app.db.session import engine
from app.models import idempotent_operation, mapping  # noqa: F401


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
