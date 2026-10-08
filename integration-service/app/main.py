from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.exceptions import ConflictError, NotFoundError, UpstreamError
from app.idempotency import IdempotencyConflict
from app.logging_config import configure_logging
from app.middleware import CorrelationIdMiddleware
from app.routers import accounts, admin, clients, loans, reconciliation, transactions
from app.schemas.common import ErrorResponse

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.db.init_db import init_db
    from app.db.session import SessionLocal
    from app.recovery import run_recovery_sweep

    init_db()
    # Part J scenario 5 (crash recovery): whatever was left mid-flight by a
    # prior run -- crashed, killed, or just restarted -- gets one recovery
    # pass right here, before the app starts accepting traffic again.
    db = SessionLocal()
    try:
        run_recovery_sweep(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Integration Service",
    description=(
        "Bridges the Mock ERP and Fineract: GL account sync (Part A), client/loan "
        "lifecycle (Parts C/D), and the external_reference idempotency contract (Part F)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(accounts.router)
app.include_router(clients.router)
app.include_router(loans.router)
app.include_router(transactions.router)
app.include_router(reconciliation.router)
app.include_router(admin.router)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@app.exception_handler(ConflictError)
@app.exception_handler(IdempotencyConflict)
async def conflict_handler(request: Request, exc: ConflictError | IdempotencyConflict) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=ErrorResponse(error_code="CONFLICT", message=exc.message, correlation_id=_correlation_id(request)).model_dump(),
    )


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=ErrorResponse(error_code="NOT_FOUND", message=exc.message, correlation_id=_correlation_id(request)).model_dump(),
    )


@app.exception_handler(UpstreamError)
async def upstream_handler(request: Request, exc: UpstreamError) -> JSONResponse:
    body = ErrorResponse(error_code="UPSTREAM_ERROR", message=exc.message, correlation_id=_correlation_id(request))
    return JSONResponse(status_code=status.HTTP_502_BAD_GATEWAY, content=body.model_dump())


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error_code="VALIDATION_ERROR", message=str(exc.errors()), correlation_id=_correlation_id(request)
        ).model_dump(),
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "UP"}
