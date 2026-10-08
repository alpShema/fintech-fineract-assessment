"""Seeds the chart of accounts. Idempotent -- safe to run on every container
startup, re-run won't create duplicates or error.

Codes and external_references are deliberately identical to the GL accounts
already created in Fineract during Part B's setup
(scripts/setup-fineract-environment.sh): 1100/GL-1100, 1200/GL-1200,
4000/GL-4000. When the Integration Service's GL sync (Part A's Python side)
runs against these same values, it will find Fineract's accounts already
there via the idempotent-return path, not create duplicates -- the whole
system agrees on one chart of accounts, not two separate ones that happen to
look similar.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.init_db import init_db
from app.db.session import SessionLocal
from app.models.account import Account, AccountType

logger = logging.getLogger(__name__)

SEED_ACCOUNTS: list[dict[str, str]] = [
    {"code": "1100", "name": "Bank", "type": AccountType.ASSET, "currency": "RWF", "external_reference": "GL-1100"},
    {"code": "1200", "name": "Loan Receivable", "type": AccountType.ASSET, "currency": "RWF", "external_reference": "GL-1200"},
    {"code": "4000", "name": "Interest Income", "type": AccountType.INCOME, "currency": "RWF", "external_reference": "GL-4000"},
]


def seed(db: Session) -> None:
    for spec in SEED_ACCOUNTS:
        exists = db.execute(select(Account).where(Account.code == spec["code"])).scalar_one_or_none()
        if exists is not None:
            continue
        db.add(Account(**spec))
    db.commit()


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()


if __name__ == "__main__":
    main()
