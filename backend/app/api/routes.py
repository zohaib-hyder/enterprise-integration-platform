from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from app.db.session import check_database
from app.schemas.system import HealthResponse, ReadinessResponse, SystemInfo

VERSION = "0.1.0"
router = APIRouter()
v1 = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return HealthResponse(service=request.app.state.settings.app_name, version=VERSION)


@router.get(
    "/ready", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}}
)
async def ready(request: Request) -> JSONResponse:
    try:
        available = await run_in_threadpool(check_database, request.app.state.engine)
    except SQLAlchemyError:
        available = False
    body = ReadinessResponse(
        status="ready" if available else "unavailable",
        checks={"database": "up" if available else "down"},
    )
    return JSONResponse(body.model_dump(), status_code=200 if available else 503)


@v1.get("/system/info", response_model=SystemInfo)
def system_info(request: Request) -> SystemInfo:
    settings = request.app.state.settings
    return SystemInfo(name=settings.app_name, version=VERSION, environment=settings.app_env)
