import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.ai import router as ai_router
from app.api.canonical import router as canonical_router
from app.api.identity import router as identity_router
from app.api.integrations import router as integration_router
from app.api.notifications import calendar as calendar_router
from app.api.notifications import router as notification_router
from app.api.operations import router as operations_router
from app.api.reliability import router as reliability_router
from app.api.routes import VERSION, router, v1
from app.api.synchronization import router as sync_router
from app.api.webhooks import router as webhook_router
from app.api.workflows import router as workflow_router
from app.connectors.contracts import ConnectorError, ErrorCategory
from app.core.config import Settings
from app.core.http_security import RequestBodyLimit
from app.core.webhook_security import IngestionError
from app.db.session import build_engine
from app.transformations.engine import TransformationFailure

logger = logging.getLogger("platform.requests")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.engine = build_engine(config)
        try:
            yield
        finally:
            if application.state.engine is not None:
                application.state.engine.dispose()

    application = FastAPI(
        title=config.app_name,
        debug=config.app_debug,
        version=VERSION,
        lifespan=lifespan,
        docs_url="/docs" if config.app_env != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if config.app_env != "production" else None,
    )
    application.state.settings = config
    application.state.engine = None
    application.state.csrf_secret = (
        config.csrf_secret.get_secret_value().encode()
        if config.csrf_secret
        else secrets.token_bytes(32)
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["X-Correlation-ID"],
        expose_headers=["X-Correlation-ID"],
    )

    application.add_middleware(RequestBodyLimit, webhook_limit=config.webhook_requests_per_minute)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=config.allowed_hosts)

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        started = time.perf_counter()
        try:
            correlation_id = str(uuid.UUID(request.headers.get("X-Correlation-ID", "")))
        except ValueError:
            correlation_id = str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - final HTTP boundary must redact all failures
            # Never log exception strings, request bodies, queries, or connection URLs.
            response = JSONResponse(
                {"error": "internal_server_error", "correlation_id": correlation_id},
                status_code=500,
            )
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            )

        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "method": request.method,
                    "route": getattr(route, "path", "unmatched"),
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "correlation_id": correlation_id,
                    **(
                        {"webhook_outcome": request.state.webhook_outcome}
                        if hasattr(request.state, "webhook_outcome")
                        else {}
                    ),
                }
            )
        )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": "invalid_request", "correlation_id": request.state.correlation_id},
            status_code=422,
        )

    @application.exception_handler(ConnectorError)
    async def connector_error(request: Request, exc: ConnectorError) -> JSONResponse:
        statuses = {
            ErrorCategory.AUTHENTICATION_FAILED: 401,
            ErrorCategory.AUTHORIZATION_FAILED: 403,
            ErrorCategory.RATE_LIMITED: 429,
            ErrorCategory.TIMEOUT: 504,
            ErrorCategory.TEMPORARY_UNAVAILABLE: 503,
            ErrorCategory.VALIDATION_FAILED: 422,
            ErrorCategory.NOT_FOUND: 404,
            ErrorCategory.CONFLICT: 409,
            ErrorCategory.UNSUPPORTED_OPERATION: 400,
            ErrorCategory.INTERNAL_ERROR: 500,
        }
        return JSONResponse(
            {
                "detail": exc.safe_message,
                "error": {"category": exc.category.value, "retryable": exc.retryable},
                "correlation_id": request.state.correlation_id,
            },
            status_code=statuses[exc.category],
        )

    @application.exception_handler(IngestionError)
    async def ingestion_error(request: Request, exc: IngestionError) -> JSONResponse:
        request.state.webhook_outcome = exc.category.value
        return JSONResponse(
            {
                "detail": "Webhook request rejected",
                "category": exc.category.value,
                "correlation_id": request.state.correlation_id,
            },
            status_code=exc.status,
        )

    @application.exception_handler(TransformationFailure)
    async def transformation_error(request: Request, exc: TransformationFailure) -> JSONResponse:
        return JSONResponse(
            {
                "detail": "Transformation input rejected",
                "issues": [issue.model_dump() for issue in exc.issues],
                "correlation_id": request.state.correlation_id,
            },
            status_code=422,
        )

    application.include_router(ai_router)
    application.include_router(sync_router)
    application.include_router(notification_router)
    application.include_router(operations_router)
    application.include_router(calendar_router)
    application.include_router(reliability_router)
    application.include_router(workflow_router)
    application.include_router(canonical_router)
    application.include_router(webhook_router)
    application.include_router(integration_router)
    application.include_router(router)
    application.include_router(v1)
    application.include_router(identity_router)
    return application


app = create_app()
