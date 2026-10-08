import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.exceptions import ConflictError, NotFoundError
from app.models.account import Account
from app.models.journal_entry import JournalEntryLine
from app.schemas.account import AccountBalance, AccountCreate, AccountRead

router = APIRouter(prefix="/api/accounts", tags=["accounts"])
logger = logging.getLogger(__name__)


def _same_payload(existing: Account, payload: AccountCreate) -> bool:
    """Explicit field-by-field comparison -- same reasoning as Fineract's
    isSamePayload() in Part A: no generic "diff and mutate" helper, just reads,
    so there's no risk of a read-only idempotency check accidentally writing
    anything."""
    return (
        existing.code == payload.code
        and existing.name == payload.name
        and existing.type == payload.type
        and existing.currency == payload.currency.upper()
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_account(payload: AccountCreate, request: Request, response: Response, db: Session = Depends(get_db)) -> AccountRead:
    correlation_id = getattr(request.state, "correlation_id", None)
    existing = db.execute(select(Account).where(Account.external_reference == payload.external_reference)).scalar_one_or_none()
    if existing is not None:
        if _same_payload(existing, payload):
            logger.info(
                "account create: idempotent return",
                extra={
                    "correlation_id": correlation_id,
                    "external_reference": payload.external_reference,
                    "status": "returned_existing",
                },
            )
            response.status_code = status.HTTP_200_OK
            return AccountRead.model_validate(existing)
        logger.warning(
            "account create: conflict",
            extra={"correlation_id": correlation_id, "external_reference": payload.external_reference, "status": "conflict"},
        )
        raise ConflictError(
            f"An account with external_reference '{payload.external_reference}' already exists with a different payload"
        )

    account = Account(
        code=payload.code,
        name=payload.name,
        type=payload.type,
        currency=payload.currency.upper(),
        external_reference=payload.external_reference,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Last line of defense against the same create-check-then-insert race
        # described in Fineract's Part A DECISIONS.md: two concurrent requests for
        # the same external_reference (or code) can both pass the check above
        # before either commits. The DB's unique constraints are the real
        # guarantee; this just converts the resulting IntegrityError into the
        # same 409 a non-concurrent conflict would produce.
        #
        # Deliberately NOT parsing exc.orig's message string to figure out which
        # constraint fired: Postgres and SQLite (used in tests) format that
        # message completely differently (Postgres names the constraint,
        # e.g. "accounts_code_key"; SQLite just says "UNIQUE constraint failed:
        # accounts.code"), so string-matching one format silently breaks on the
        # other. Re-querying for what actually collided is dialect-agnostic.
        if db.execute(select(Account).where(Account.external_reference == payload.external_reference)).scalar_one_or_none():
            raise ConflictError(
                f"An account with external_reference '{payload.external_reference}' already exists with a different payload"
            ) from exc
        if db.execute(select(Account).where(Account.code == payload.code)).scalar_one_or_none():
            raise ConflictError(f"An account with code '{payload.code}' already exists") from exc
        raise
    db.refresh(account)
    logger.info(
        "account create: created",
        extra={"correlation_id": correlation_id, "external_reference": payload.external_reference, "status": "created"},
    )
    return AccountRead.model_validate(account)


@router.get("")
def list_accounts(db: Session = Depends(get_db)) -> list[AccountRead]:
    accounts = db.execute(select(Account).order_by(Account.code)).scalars().all()
    return [AccountRead.model_validate(a) for a in accounts]


@router.get("/{code}/balance")
def get_account_balance(code: str, db: Session = Depends(get_db)) -> AccountBalance:
    account = db.execute(select(Account).where(Account.code == code)).scalar_one_or_none()
    if account is None:
        raise NotFoundError(f"No account with code '{code}'")

    totals = db.execute(
        select(
            JournalEntryLine.debit,
            JournalEntryLine.credit,
        ).where(JournalEntryLine.account_id == account.id)
    ).all()
    total_debit = sum((row.debit for row in totals), Decimal("0"))
    total_credit = sum((row.credit for row in totals), Decimal("0"))

    balance = (total_debit - total_credit) if account.type.normal_balance_is_debit else (total_credit - total_debit)
    return AccountBalance(code=account.code, currency=account.currency, balance=balance)
