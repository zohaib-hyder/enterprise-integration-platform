from pathlib import Path

files = {}
def add(path, content): files[path] = content.lstrip('\n')
for package in ['app', 'app/api', 'app/core', 'app/db', 'app/models', 'app/schemas', 'app/services', 'app/connectors', 'app/workflows', 'app/workers']:
    add(f'backend/{package}/__init__.py', '')
add('.gitignore', '''.env
.env.local
.env.*.local
!.env.example
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
node_modules/
.next/
coverage/
*.tsbuildinfo
.DS_Store
''')
add('backend/pyproject.toml', '''[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "enterprise-integration-backend"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["fastapi>=0.115,<1", "uvicorn>=0.34,<1", "pydantic-settings>=2.7,<3", "sqlalchemy>=2.0,<3", "alembic>=1.14,<2", "psycopg[binary]>=3.2,<4"]

[project.optional-dependencies]
dev = ["pytest>=8,<10", "httpx>=0.28,<1", "ruff>=0.9,<1"]

[tool.setuptools.packages.find]
include = ["app*"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
target-version = "py311"
line-length = 100
''')
add('backend/.env.example', '''APP_ENV=development
APP_NAME=Northstar Integration Platform
# Set a PostgreSQL URL with the credentials you chose in the root .env.
DATABASE_URL=postgresql+psycopg://YOUR_USER:YOUR_PASSWORD@127.0.0.1:5432/integration
CORS_ORIGINS=["http://localhost:3000"]
''')
add('.env.example', '''POSTGRES_USER=integration
POSTGRES_PASSWORD=
POSTGRES_DB=integration
POSTGRES_PORT=5432
''')
add('compose.yaml', '''services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER:?Set POSTGRES_USER in .env}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?Set POSTGRES_PASSWORD in .env}
      POSTGRES_DB: ${POSTGRES_DB:-integration}
    ports:
      - "127.0.0.1:${POSTGRES_PORT:-5432}:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 10s
      timeout: 3s
      retries: 5
    mem_limit: 256m
    cpus: 1.0
volumes:
  postgres_data:
''')
add('backend/app/core/config.py', '''from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_name: str = "Northstar Integration Platform"
    app_env: Literal["development", "test", "production"] = "development"
    database_url: SecretStr | None = None
    cors_origins: list[str] = []

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                url = make_url(value.get_secret_value())
                if url.drivername != "postgresql+psycopg" or not url.database or not url.host:
                    raise ValueError
            except Exception:
                raise ValueError("A PostgreSQL psycopg connection URL is required") from None
        return value

    @field_validator("cors_origins")
    @classmethod
    def validate_origins(cls, origins: list[str]) -> list[str]:
        for origin in origins:
            parsed = urlsplit(origin)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment or "*" in origin):
                raise ValueError("CORS must contain explicit HTTP(S) origins without paths")
        return origins

    @model_validator(mode="after")
    def production_safety(self) -> "Settings":
        if self.app_env == "production":
            if self.database_url is None:
                raise ValueError("Production requires database configuration")
            if any(not origin.startswith("https://") for origin in self.cors_origins):
                raise ValueError("Production CORS origins require HTTPS")
        return self
''')
add('backend/app/db/session.py', '''from sqlalchemy import Engine, create_engine, text
from app.core.config import Settings


def build_engine(settings: Settings) -> Engine | None:
    if settings.database_url is None:
        return None
    return create_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True,
        pool_size=2, max_overflow=2, connect_args={"connect_timeout": 3},
    )


def check_database(engine: Engine | None) -> bool:
    if engine is None:
        return False
    with engine.connect() as connection:
        return connection.scalar(text("SELECT 1")) == 1
''')
add('backend/app/db/base.py', '''from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
''')
add('backend/app/models/tenant.py', '''import uuid
from datetime import datetime
from sqlalchemy import DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column
from app.db.base import Base


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
''')
add('backend/app/schemas/system.py', '''from typing import Literal
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["healthy"] = "healthy"
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, Literal["up", "down"]]


class SystemInfo(BaseModel):
    name: str
    version: str
    api_version: str = "v1"
    environment: str
    phase: int = 0
''')
add('backend/app/api/routes.py', '''from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from app.db.session import check_database
from app.schemas.system import HealthResponse, ReadinessResponse, SystemInfo

VERSION = "0.1.0"
router = APIRouter()
v1 = APIRouter(prefix="/api/v1")


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    return HealthResponse(service=request.app.state.settings.app_name, version=VERSION)


@router.get("/ready", response_model=ReadinessResponse, responses={503: {"model": ReadinessResponse}})
async def ready(request: Request) -> JSONResponse:
    try:
        available = await run_in_threadpool(check_database, request.app.state.engine)
    except Exception:
        available = False
    body = ReadinessResponse(status="ready" if available else "unavailable",
                             checks={"database": "up" if available else "down"})
    return JSONResponse(body.model_dump(), status_code=200 if available else 503)


@v1.get("/system/info", response_model=SystemInfo)
def system_info(request: Request) -> SystemInfo:
    settings = request.app.state.settings
    return SystemInfo(name=settings.app_name, version=VERSION, environment=settings.app_env)
''')
add('backend/app/main.py', '''import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.api.routes import router, v1, VERSION
from app.core.config import Settings
from app.db.session import build_engine

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

    application = FastAPI(title=config.app_name, version=VERSION, lifespan=lifespan,
                          docs_url="/docs" if config.app_env != "production" else None,
                          redoc_url=None,
                          openapi_url="/openapi.json" if config.app_env != "production" else None)
    application.state.settings = config
    application.state.engine = None
    application.add_middleware(CORSMiddleware, allow_origins=config.cors_origins,
                              allow_credentials=False, allow_methods=["GET"],
                              allow_headers=["X-Correlation-ID"],
                              expose_headers=["X-Correlation-ID"])

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
        except Exception:
            # Never log exception strings, request bodies, queries, or connection URLs.
            response = JSONResponse({"error": "internal_server_error",
                                     "correlation_id": correlation_id}, status_code=500)
        response.headers["X-Correlation-ID"] = correlation_id
        route = request.scope.get("route")
        logger.info(json.dumps({"event": "http_request", "method": request.method,
                                "route": getattr(route, "path", "unmatched"),
                                "status": response.status_code,
                                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                                "correlation_id": correlation_id}))
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"error": "invalid_request",
                             "correlation_id": request.state.correlation_id}, status_code=422)

    application.include_router(router)
    application.include_router(v1)
    return application


app = create_app()
''')
add('backend/alembic.ini', '''[alembic]
script_location = %(here)s/alembic
prepend_sys_path = .
''')
add('backend/alembic/env.py', '''from alembic import context
from sqlalchemy import create_engine, pool
from app.core.config import Settings
from app.db.base import Base
from app.models.tenant import Tenant  # noqa: F401

settings = Settings()
if settings.database_url is None:
    raise RuntimeError("DATABASE_URL is required for migrations")
url = settings.database_url.get_secret_value()
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      compare_type=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
''')
add('backend/alembic/script.py.mako', '''"""${message}"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}
revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}

def upgrade() -> None:
    ${upgrades if upgrades else "pass"}

def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
''')
add('backend/alembic/versions/0001_tenants.py', '''"""Create minimal tenant registry."""
from alembic import op
import sqlalchemy as sa

revision = "0001_tenants"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"))
    op.create_index("ix_tenants_slug", "tenants", ["slug"], unique=True)
    op.create_index("ix_tenants_created_at", "tenants", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_tenants_created_at", table_name="tenants")
    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
''')
add('backend/tests/test_system.py', '''import uuid
from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def client():
    with TestClient(create_app(Settings(_env_file=None, app_env="test"))) as client:
        yield client


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    uuid.UUID(response.headers["X-Correlation-ID"])


def test_info_is_safe(client):
    body = client.get("/api/v1/system/info").json()
    assert body == {"name": "Northstar Integration Platform", "version": "0.1.0",
                    "api_version": "v1", "environment": "test", "phase": 0}


def test_readiness_without_database(client):
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"database": "down"}}


def test_database_failure_is_redacted(client):
    with patch("app.api.routes.check_database", side_effect=RuntimeError("secret-password")):
        response = client.get("/ready")
    assert response.status_code == 503
    assert "secret-password" not in response.text


def test_readiness_success(client):
    with patch("app.api.routes.check_database", return_value=True):
        assert client.get("/ready").json()["status"] == "ready"


@pytest.mark.parametrize("origin", ["*", "https://*.example.com", "http://localhost:3000/path",
                                    "https://user:pass@example.com"])
def test_unsafe_cors_rejected(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins=[origin])


def test_secure_defaults_and_secret_repr():
    config = Settings(_env_file=None)
    assert config.cors_origins == []
    assert config.database_url is None
    secret = Settings(_env_file=None, database_url="postgresql+psycopg://user:secret@localhost/db")
    assert "secret@" not in repr(secret)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")


def test_correlation_id_and_logs(client, capsys):
    correlation = str(uuid.uuid4())
    response = client.get("/health?token=private", headers={"X-Correlation-ID": correlation})
    assert response.headers["X-Correlation-ID"] == correlation
    assert "private" not in capsys.readouterr().err


def test_safe_unhandled_error(client):
    @client.app.get("/test-error")
    def fail():
        raise RuntimeError("private")
    response = client.get("/test-error")
    assert response.status_code == 500
    assert response.json()["error"] == "internal_server_error"
    assert "private" not in response.text
''')
add('frontend/package.json', '''{
  "name": "enterprise-integration-frontend", "version": "0.1.0", "private": true,
  "scripts": {"dev": "next dev --hostname 127.0.0.1", "build": "next build --webpack", "start": "next start --hostname 127.0.0.1", "lint": "eslint .", "typecheck": "tsc --noEmit", "test": "vitest run"},
  "dependencies": {"next": "^16.0.0", "react": "^19.2.0", "react-dom": "^19.2.0"},
  "devDependencies": {"@tailwindcss/postcss": "^4.1.0", "tailwindcss": "^4.1.0", "typescript": "^5.9.0", "@types/node": "^24.0.0", "@types/react": "^19.2.0", "@types/react-dom": "^19.2.0", "eslint": "^9.0.0", "eslint-config-next": "^16.0.0", "vitest": "^3.2.0", "@testing-library/react": "^16.3.0", "@testing-library/jest-dom": "^6.9.0", "jsdom": "^26.1.0"}
}
''')
add('frontend/.env.example', 'BACKEND_URL=http://127.0.0.1:8000\n')
add('frontend/tsconfig.json', '''{"compilerOptions":{"target":"ES2017","lib":["dom","dom.iterable","esnext"],"allowJs":true,"skipLibCheck":true,"strict":true,"noEmit":true,"esModuleInterop":true,"module":"esnext","moduleResolution":"bundler","resolveJsonModule":true,"isolatedModules":true,"jsx":"react-jsx","incremental":true,"plugins":[{"name":"next"}],"paths":{"@/*":["./*"]}},"include":["next-env.d.ts","**/*.ts","**/*.tsx",".next/types/**/*.ts",".next/dev/types/**/*.ts"],"exclude":["node_modules"]}
''')
add('frontend/next-env.d.ts', '/// <reference types="next" />\n/// <reference types="next/image-types/global" />\n')
add('frontend/next.config.ts', '''import type { NextConfig } from "next";
const config: NextConfig = { poweredByHeader: false };
export default config;
''')
add('frontend/eslint.config.mjs', '''import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";
export default defineConfig([...nextVitals, ...nextTs, globalIgnores([".next/**", "next-env.d.ts"])]);
''')
add('frontend/postcss.config.mjs', 'export default { plugins: { "@tailwindcss/postcss": {} } };\n')
add('frontend/vitest.config.ts', '''import { defineConfig } from "vitest/config";
export default defineConfig({ test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"] } });
''')
add('frontend/tests/setup.ts', 'import "@testing-library/jest-dom/vitest";\n')
add('frontend/app/layout.tsx', '''import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Northstar | Integration Platform", description: "Staffing operations integration foundation" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
''')
add('frontend/app/globals.css', '''@import "tailwindcss";
:root { color-scheme: light; }
body { margin: 0; background: #f5f7fb; color: #17233b; font-family: Arial, Helvetica, sans-serif; }
button { cursor: pointer; }
button:disabled { cursor: not-allowed; }
:focus-visible { outline: 3px solid #60a5fa; outline-offset: 4px; }
''')
add('frontend/app/api/health/route.ts', '''import { NextResponse } from "next/server";
export const dynamic = "force-dynamic";
export async function GET() {
  try {
    const base = new URL(process.env.BACKEND_URL ?? "http://127.0.0.1:8000");
    if (!["http:", "https:"].includes(base.protocol) || base.username || base.password) throw new Error("Invalid backend URL");
    const response = await fetch(new URL("/health", base), { cache: "no-store", signal: AbortSignal.timeout(4000), redirect: "error" });
    const body: unknown = await response.json();
    if (!response.ok || typeof body !== "object" || body === null || !("status" in body) || body.status !== "healthy") throw new Error("Unhealthy");
    return NextResponse.json({ status: "healthy" });
  } catch {
    return NextResponse.json({ status: "unavailable" }, { status: 503 });
  }
}
''')
add('frontend/components/dashboard.tsx', '''"use client";
import { useCallback, useEffect, useState } from "react";
const modules = ["Overview", "Connections", "Workflows", "Events", "Executions", "Failures", "Mappings", "Alerts", "Audit", "Settings"];
type Status = "loading" | "healthy" | "unavailable";
export default function Dashboard() {
  const [status, setStatus] = useState<Status>("loading");
  const [checking, setChecking] = useState(false);
  const check = useCallback(async (signal?: AbortSignal) => {
    setChecking(true);
    try {
      const response = await fetch("/api/health", { cache: "no-store", signal: signal ?? AbortSignal.timeout(6000) });
      const body = await response.json();
      if (!signal?.aborted) setStatus(response.ok && body.status === "healthy" ? "healthy" : "unavailable");
    } catch { if (!signal?.aborted) setStatus("unavailable"); }
    finally { if (!signal?.aborted) setChecking(false); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void check(controller.signal);
    const timer = setInterval(() => void check(controller.signal), 30000);
    return () => { controller.abort(); clearInterval(timer); };
  }, [check]);
  return <div className="min-h-screen lg:flex">
    <aside className="bg-[#101e36] px-6 py-8 text-slate-300 lg:fixed lg:inset-y-0 lg:w-64">
      <a href="/" className="flex items-center gap-3 text-xl font-bold tracking-tight text-white"><span className="rounded-lg bg-blue-500 px-3 py-2">N</span> Northstar</a>
      <p className="mt-3 text-xs uppercase tracking-[.18em] text-slate-400">Integration workspace</p>
      <nav aria-label="Main navigation" className="mt-10 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-1">
        {modules.map((item, index) => index === 0 ? <a key={item} href="/" aria-current="page" className="rounded-lg bg-blue-500/20 px-4 py-3 text-sm font-semibold text-blue-200">◈ &nbsp; {item}</a> : <button key={item} disabled className="flex items-center justify-between rounded-lg px-4 py-3 text-left text-sm text-slate-400"><span>{item}</span><span className="ml-2 text-[10px] text-slate-500">Coming soon</span></button>)}
      </nav>
      <p className="mt-10 border-t border-slate-700 pt-5 text-xs leading-5 text-slate-400">Portfolio environment<br/>Synthetic company · Phase 0</p>
    </aside>
    <main className="flex-1 lg:ml-64">
      <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-5 md:px-10"><span className="text-sm text-slate-500">Workspace / <strong className="font-medium text-slate-800">Overview</strong></span><span className="rounded-full border border-slate-200 px-3 py-1 text-xs text-slate-600">Foundation preview</span></header>
      <div className="mx-auto max-w-6xl px-6 py-10 md:px-10">
        <p className="text-xs font-bold uppercase tracking-[.2em] text-blue-600">Operations, connected</p>
        <h1 className="mt-3 text-3xl font-bold tracking-tight md:text-4xl">Workspace overview</h1>
        <p className="mt-4 max-w-2xl text-base leading-7 text-slate-500">A shared foundation for Northstar’s staffing systems. Connectors and workflows will arrive in later phases; no operational data is available yet.</p>
        <section aria-label="API connectivity" className="mt-8 flex flex-wrap items-center justify-between gap-5 rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
          <div><h2 className="font-semibold">Platform API</h2><p className="mt-2 text-sm text-slate-500">Live health check · refreshed every 30 seconds</p><p className="mt-1 text-xs text-slate-500">API health does not confirm database readiness.</p></div>
          <div className="flex items-center gap-4"><p role="status" aria-live="polite" className={`rounded-full px-3 py-2 text-sm font-medium ${status === "healthy" ? "bg-emerald-50 text-emerald-700" : status === "unavailable" ? "bg-amber-50 text-amber-800" : "bg-slate-100 text-slate-600"}`}>{status === "loading" ? "Checking API…" : status === "healthy" ? "API connected" : "API unavailable"}</p><button disabled={checking} onClick={() => void check()} className="rounded-lg border border-slate-300 px-3 py-2 text-sm disabled:opacity-50">{checking ? "Checking…" : "Retry"}</button></div>
          {status === "unavailable" && <p className="w-full text-sm text-amber-800">The API could not be reached. Start the backend and check the frontend server configuration, then retry.</p>}
        </section>
        <div className="mt-8 grid gap-6 md:grid-cols-3">{[["01", "Connect your systems", "CRM, ATS, calendar, email and Slack boundaries will be modeled with local simulators."], ["02", "Coordinate workflows", "Durable event processing, mapping and recovery are planned for later phases."], ["03", "Make work visible", "Execution history and audit trails will show real activity once implemented."]].map(([number, title, text]) => <section key={number} className="rounded-xl border border-slate-200 bg-white p-6"><span className="text-sm font-semibold text-blue-600">{number} / Planned</span><h2 className="mt-5 text-lg font-semibold">{title}</h2><p className="mt-3 text-sm leading-6 text-slate-500">{text}</p></section>)}</div>
        <section className="mt-8 rounded-xl border border-dashed border-slate-300 p-8"><h2 className="text-lg font-semibold">An honest starting point</h2><p className="mt-3 max-w-3xl text-sm leading-6 text-slate-500">This phase establishes the application shell, API diagnostics and tenant database schema. There are no connected providers, active workflows or execution metrics. Unfinished modules are marked coming soon.</p></section>
        <footer className="mt-8 text-xs text-slate-400">Northstar Staffing · Fictional North American staffing company · v0.1.0</footer>
      </div>
    </main>
  </div>;
}
''')
add('frontend/app/page.tsx', 'import Dashboard from "../components/dashboard";\nexport default function Home() { return <Dashboard />; }\n')
add('frontend/app/loading.tsx', 'export default function Loading() { return <main className="p-10" role="status">Loading workspace…</main>; }\n')
add('frontend/app/error.tsx', '''"use client";
export default function Error({ reset }: { reset: () => void }) { return <main className="p-10"><h1 className="text-2xl font-bold">Workspace could not load</h1><button className="mt-4 rounded bg-blue-600 px-4 py-2 text-white" onClick={reset}>Try again</button></main>; }
''')
add('frontend/tests/dashboard.test.tsx', '''import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import Dashboard from "../components/dashboard";
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
test("initial state is honest and modules are disabled", () => {
  vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
  render(<Dashboard />);
  expect(screen.getByRole("heading", { name: "Workspace overview" })).toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Checking API");
  expect(screen.getByRole("button", { name: /Connections/ })).toBeDisabled();
  expect(screen.getByText(/no operational data is available yet/)).toBeInTheDocument();
});
test("successful health response updates connectivity", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: "healthy" }) }));
  render(<Dashboard />);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API connected"));
});
test("network failure gives a retryable error", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
  render(<Dashboard />);
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("API unavailable"));
  expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
});
''')
for directory in ['docs', '.github/workflows']:
    Path(directory).mkdir(parents=True, exist_ok=True)
add('.github/workflows/.gitkeep', '')
for path, content in files.items():
    target = Path(path)
    if target.exists():
        raise RuntimeError(f'Refusing to overwrite {path}')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
