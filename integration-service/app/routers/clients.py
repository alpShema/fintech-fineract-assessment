import logging
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_api_key
from app.clients.fineract import FineractClient, FineractError
from app.db.session import get_db
from app.exceptions import ConflictError, NotFoundError, UpstreamError
from app.fineract_dates import FINERACT_DATE_FORMAT, format_fineract_date
from app.idempotency import check_idempotency, complete_operation, fail_operation, start_operation
from app.models.idempotent_operation import OperationType
from app.models.mapping import ClientMapping
from app.schemas.client import ClientCreate, ClientRead

router = APIRouter(prefix="/api/clients", tags=["clients"], dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)

FINERACT_OFFICE_ID = 2  # Kigali Main Office, created by scripts/setup-fineract-environment.sh (Part B)
FINERACT_LEGAL_FORM_PERSON = 1


@router.post("", status_code=201)
def create_client(payload: ClientCreate, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    correlation_fields = {"external_reference": payload.external_reference, "client_uid": payload.client_uid}

    decision = check_idempotency(db, OperationType.CLIENT_CREATE, payload.external_reference, request_payload)
    if not decision.is_new:
        response.status_code = decision.replay_status_code or 200
        return decision.replay_body or {}

    # Part F: "A different reference may create another client only if its
    # client_uid is also different; client_uid is unique at the database
    # level, so reuse is rejected with 409." Checked here, before ever calling
    # Fineract, so a genuinely conflicting request doesn't create an orphaned
    # Fineract client first.
    existing_mapping = db.execute(
        select(ClientMapping).where(ClientMapping.client_uid == payload.client_uid)
    ).scalar_one_or_none()
    if existing_mapping is not None:
        raise ConflictError(f"client_uid '{payload.client_uid}' is already in use by another client")

    operation = start_operation(db, OperationType.CLIENT_CREATE, payload.external_reference, request_payload)

    fineract_payload = {
        "officeId": FINERACT_OFFICE_ID,
        "firstname": payload.firstname,
        "lastname": payload.lastname,
        "externalId": payload.client_uid,
        "legalFormId": FINERACT_LEGAL_FORM_PERSON,
        "active": True,
        "activationDate": format_fineract_date(date.today()),
        "dateFormat": FINERACT_DATE_FORMAT,
        "locale": "en",
    }
    fineract = FineractClient()
    try:
        result = fineract.create_client(fineract_payload)
    except FineractError as exc:
        fail_operation(db, operation, exc.status_code, {"error": str(exc.body)})
        logger.warning("client create failed at Fineract", extra={**correlation_fields, "status": "FAILED"})
        raise UpstreamError(f"Fineract client creation failed: {exc.body}") from exc
    finally:
        fineract.close()

    fineract_client_id = result["resourceId"]
    try:
        db.add(ClientMapping(client_uid=payload.client_uid, fineract_client_id=fineract_client_id))
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Race: another request claimed this client_uid between our check above
        # and this insert. The Fineract client above was already created and is
        # now orphaned (no local mapping) -- Part J's reconciliation/recovery
        # mechanism is the proper fix for this; for now, surface it loudly
        # rather than silently losing track of it.
        fail_operation(db, operation, 409, {"error": "client_uid race lost", "orphaned_fineract_client_id": fineract_client_id})
        logger.error(
            "client_uid race condition -- Fineract client orphaned",
            extra={**correlation_fields, "status": "FAILED"},
        )
        raise ConflictError(f"client_uid '{payload.client_uid}' is already in use by another client") from exc

    response_body = {
        "client_uid": payload.client_uid,
        "status": "Active",
        "display_name": f"{payload.firstname} {payload.lastname}",
    }
    complete_operation(db, operation, 201, response_body)
    logger.info("client created", extra={**correlation_fields, "status": "COMPLETED"})
    return response_body


@router.get("/{client_uid}")
def get_client(client_uid: str) -> ClientRead:
    fineract = FineractClient()
    try:
        result = fineract.get_client_by_external_id(client_uid)
    finally:
        fineract.close()
    if result is None:
        raise NotFoundError(f"No client with client_uid '{client_uid}'")
    return ClientRead(client_uid=client_uid, status=result["status"]["value"], display_name=result["displayName"])
