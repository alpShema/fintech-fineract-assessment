"""Creates tables from the ORM models if they don't already exist.

No migration tool (Alembic etc.) for this service, deliberately: the schema is
small, stable, and this is a mock service for an assessment, not a production
system undergoing repeated schema evolution. `create_all` is idempotent (it
skips tables that already exist) and keeps setup scripted/repeatable without
the added machinery a migration tool brings for marginal benefit here. See
DECISIONS.md.
"""

from app.db.base import Base
from app.db.session import engine
from app.models import account, journal_entry  # noqa: F401  (import registers the models with Base.metadata)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
