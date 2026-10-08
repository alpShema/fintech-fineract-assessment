import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.session import get_db
from app.exceptions import ConflictError, NotFoundError
from app.models.account import Account
from app.models.journal_entry import JournalEntry, JournalEntryLine
from app.schemas.journal_entry import JournalEntryCreate, JournalEntryLineRead, JournalEntryRead

router = APIRouter(prefix="/api/journal-entries", tags=["journal-entries"])
logger = logging.getLogger(__name__)


def _to_read_model(entry: JournalEntry, code_by_account_id: dict[int, str]) -> JournalEntryRead:
    return JournalEntryRead(
        id=entry.id,
        external_reference=entry.external_reference,
        entry_date=entry.entry_date,
        description=entry.description,
        created_at=entry.created_at,
        lines=[
            JournalEntryLineRead(account_code=code_by_account_id[line.account_id], debit=line.debit, credit=line.credit)
            for line in entry.lines
        ],
    )


def _same_payload(existing: JournalEntry, payload: JournalEntryCreate, code_by_account_id: dict[int, str]) -> bool:
    if existing.entry_date != payload.entry_date or existing.description != payload.description:
        return False
    existing_lines = {(code_by_account_id[line.account_id], line.debit, line.credit) for line in existing.lines}
    payload_lines = {(line.account_code, line.debit, line.credit) for line in payload.lines}
    return existing_lines == payload_lines


@router.post("", status_code=status.HTTP_201_CREATED)
def create_journal_entry(
    payload: JournalEntryCreate, request: Request, response: Response, db: Session = Depends(get_db)
) -> JournalEntryRead:
    correlation_id = getattr(request.state, "correlation_id", None)

    # Resolve every referenced account_code up front -- a journal entry must never
    # be partially created against accounts that don't exist.
    codes = {line.account_code for line in payload.lines}
    accounts = db.execute(select(Account).where(Account.code.in_(codes))).scalars().all()
    account_by_code = {a.code: a for a in accounts}
    missing = codes - account_by_code.keys()
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown account code(s): {sorted(missing)}")
    code_by_account_id = {a.id: a.code for a in accounts}

    existing = db.execute(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.external_reference == payload.external_reference)
    ).scalar_one_or_none()
    if existing is not None:
        if _same_payload(existing, payload, code_by_account_id):
            logger.info(
                "journal entry create: idempotent return",
                extra={
                    "correlation_id": correlation_id,
                    "external_reference": payload.external_reference,
                    "status": "returned_existing",
                },
            )
            response.status_code = status.HTTP_200_OK
            return _to_read_model(existing, code_by_account_id)
        logger.warning(
            "journal entry create: conflict",
            extra={"correlation_id": correlation_id, "external_reference": payload.external_reference, "status": "conflict"},
        )
        raise ConflictError(
            f"A journal entry with external_reference '{payload.external_reference}' already exists with a different payload"
        )

    entry = JournalEntry(
        external_reference=payload.external_reference,
        entry_date=payload.entry_date,
        description=payload.description,
    )
    entry.lines = [
        JournalEntryLine(account_id=account_by_code[line.account_code].id, debit=line.debit, credit=line.credit)
        for line in payload.lines
    ]
    db.add(entry)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Same race-condition last-line-of-defense pattern as accounts.py / Part A.
        # Re-query rather than parse exc.orig's message string -- see the comment
        # in accounts.py for why that's dialect-fragile (Postgres vs SQLite).
        conflict_exists = db.execute(
            select(JournalEntry).where(JournalEntry.external_reference == payload.external_reference)
        ).scalar_one_or_none()
        if conflict_exists:
            raise ConflictError(
                f"A journal entry with external_reference '{payload.external_reference}' already exists with a different payload"
            ) from exc
        raise
    db.refresh(entry)
    logger.info(
        "journal entry create: created",
        extra={"correlation_id": correlation_id, "external_reference": payload.external_reference, "status": "created"},
    )
    return _to_read_model(entry, code_by_account_id)


@router.get("/{external_reference}")
def get_journal_entry(external_reference: str, db: Session = Depends(get_db)) -> JournalEntryRead:
    entry = db.execute(
        select(JournalEntry)
        .options(selectinload(JournalEntry.lines))
        .where(JournalEntry.external_reference == external_reference)
    ).scalar_one_or_none()
    if entry is None:
        raise NotFoundError(f"No journal entry with external_reference '{external_reference}'")
    accounts = db.execute(select(Account)).scalars().all()
    code_by_account_id = {a.id: a.code for a in accounts}
    return _to_read_model(entry, code_by_account_id)
