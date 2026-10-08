from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db.session import SessionLocal
from app.exceptions import ConflictError, NotFoundError
from app.logging_config import configure_logging
from app.middleware import CorrelationIdMiddleware
from app.routers import accounts, journal_entries
from app.schemas.common import ErrorResponse
from app.seed import seed

configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from app.db.init_db import init_db

    init_db()
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="Mock ERP",
    description="Simulates the ERP accounting master (chart of accounts + journal entries) for the fintech assessment's Part G.",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(CorrelationIdMiddleware)
app.include_router(accounts.router)
app.include_router(journal_entries.router)


def _correlation_id(request: Request) -> str | None:
    return getattr(request.state, "correlation_id", None)


@app.exception_handler(ConflictError)
async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
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
