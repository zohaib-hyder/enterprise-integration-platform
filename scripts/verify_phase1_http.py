"""Repeatable BFF HTTP smoke check; run from backend after starting both servers.

Creates only synthetic local accounts via the operator CLI. Credentials and HTTP
cookies are never printed. Verification credentials remain in a private local file.
"""

import json
import os
import secrets
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
from app.core.config import Settings
from app.db.session import build_engine
from app.models.identity import AuditEvent, AuthSession, User
from sqlalchemy import select
from sqlalchemy.orm import Session


def main() -> None:
    backend = Path(__file__).resolve().parents[1] / "backend"
    directory = backend / ".local"
    directory.mkdir(mode=0o700, exist_ok=True)
    credentials_file = directory / "http-verification.json"
    if credentials_file.exists():
        credentials = json.loads(credentials_file.read_text())
    else:
        suffix = uuid.uuid4().hex[:12]
        credentials = {
            "email": f"demo-admin-{suffix}@example.test",
            "password": secrets.token_urlsafe(24),
            "slug_a": f"synthetic-alpha-{suffix}",
            "slug_b": f"synthetic-beta-{suffix}",
            "outsider_email": f"demo-outsider-{suffix}@example.test",
            "outsider_password": secrets.token_urlsafe(24),
            "slug_private": f"synthetic-private-{suffix}",
        }
        fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(credentials, file)
    for slug, name, outsider in [
        (credentials["slug_a"], "Synthetic HTTP Alpha", False),
        (credentials["slug_b"], "Synthetic HTTP Beta", False),
        (credentials["slug_private"], "Synthetic HTTP Private", True),
    ]:
        env = {
            **os.environ,
            "BOOTSTRAP_EMAIL": credentials["outsider_email"]
            if outsider
            else credentials["email"],
            "BOOTSTRAP_PASSWORD": credentials["outsider_password"]
            if outsider
            else credentials["password"],
            "BOOTSTRAP_DISPLAY_NAME": "Synthetic HTTP Administrator",
            "BOOTSTRAP_TENANT_NAME": name,
            "BOOTSTRAP_TENANT_SLUG": slug,
        }
        subprocess.run(
            [sys.executable, "-m", "app.bootstrap"],
            cwd=backend,
            env=env,
            check=True,
            capture_output=True,
        )
    sensitive = [credentials["password"], credentials["outsider_password"]]
    origin = "http://127.0.0.1:3000"
    with httpx.Client(base_url=origin, timeout=20) as client:

        def mutate(method: str, path: str, data: dict | None = None) -> httpx.Response:
            csrf = client.get("/api/platform/auth/csrf")
            assert csrf.status_code == 200
            return client.request(
                method,
                "/api/platform/" + path,
                json=data or {},
                headers={"Origin": origin, "X-CSRF-Token": csrf.json()["csrf_token"]},
            )

        assert client.get("/api/platform/auth/me").status_code == 401
        csrf = client.get("/api/platform/auth/csrf").json()["csrf_token"]
        assert (
            client.post(
                "/api/platform/auth/login",
                json={
                    "email": credentials["email"],
                    "password": credentials["password"],
                },
                headers={
                    "Origin": "https://attacker.example.test",
                    "X-CSRF-Token": csrf,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/platform/auth/login",
                json={
                    "email": credentials["email"],
                    "password": credentials["password"],
                },
                headers={"Origin": origin},
            ).status_code
            == 403
        )
        response = mutate(
            "POST",
            "auth/login",
            {"email": credentials["email"], "password": credentials["password"]},
        )
        assert response.status_code == 200
        session_cookie = next(
            cookie
            for cookie in response.headers.get_list("set-cookie")
            if cookie.startswith("platform_session=")
        )
        assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
        assert credentials["password"] not in response.text
        assert (
            "password_hash" not in response.text and "token_hash" not in response.text
        )
        tenants = response.json()["tenants"]
        tenant_b = next(
            tenant for tenant in tenants if tenant["slug"] == credentials["slug_b"]
        )
        assert (
            mutate("POST", "tenant/switch", {"tenant_id": tenant_b["id"]}).status_code
            == 200
        )
        assert client.get("/api/platform/tenant").json()["id"] == tenant_b["id"]
        assert (
            client.get(f"/api/platform/tenants/{tenant_b['id']}/members").status_code
            == 200
        )
        assert (
            client.get(f"/api/platform/tenants/{uuid.uuid4()}/audit").status_code == 403
        )
        assert (
            mutate(
                "POST", "tenant/switch", {"tenant_id": str(uuid.uuid4())}
            ).status_code
            == 403
        )
        # A separate account proves denial against an existing, unjoined tenant.
        with httpx.Client(base_url=origin, timeout=20) as other:
            token = other.get("/api/platform/auth/csrf").json()["csrf_token"]
            signed_in = other.post(
                "/api/platform/auth/login",
                json={
                    "email": credentials["outsider_email"],
                    "password": credentials["outsider_password"],
                },
                headers={"Origin": origin, "X-CSRF-Token": token},
            )
            assert signed_in.status_code == 200
            private_id = signed_in.json()["active_tenant_id"]
            assert (
                client.get(f"/api/platform/tenants/{private_id}/members").status_code
                == 403
            )
            assert (
                client.get(f"/api/platform/tenants/{private_id}/audit").status_code
                == 403
            )
            token = other.get("/api/platform/auth/csrf").json()["csrf_token"]
            assert (
                other.post(
                    "/api/platform/auth/logout",
                    json={},
                    headers={"Origin": origin, "X-CSRF-Token": token},
                ).status_code
                == 200
            )
        raw_session = client.cookies.get("platform_session")
        sensitive.extend([raw_session, csrf])
        assert mutate("POST", "auth/logout").status_code == 200
        assert client.get("/api/platform/auth/me").status_code == 401
        client.cookies.set("platform_session", raw_session)
        assert client.get("/api/platform/auth/me").status_code == 401
        assert client.get("/api/health").json()["status"] == "healthy"
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as api:
        for path in ["/health", "/ready", "/api/v1/system/info"]:
            assert api.get(path).status_code == 200
    engine = build_engine(Settings())
    assert engine is not None
    try:
        with Session(engine) as db:
            users = list(
                db.scalars(
                    select(User).where(
                        User.email.in_(
                            [credentials["email"], credentials["outsider_email"]]
                        )
                    )
                )
            )
            ids = [user.id for user in users]
            sensitive.extend(user.password_hash for user in users)
            sensitive.extend(
                db.scalars(
                    select(AuthSession.token_hash).where(AuthSession.user_id.in_(ids))
                )
            )
            metadata = json.dumps(
                [
                    row.details
                    for row in db.scalars(
                        select(AuditEvent).where(AuditEvent.actor_user_id.in_(ids))
                    )
                ]
            )
            assert all(value not in metadata for value in sensitive if value)
    finally:
        engine.dispose()
    logs = [
        path
        for path in [directory / "http-backend.log", directory / "http-frontend.log"]
        if path.exists()
    ]
    for path in logs:
        content = path.read_text()
        assert all(value not in content for value in sensitive if value)
    if logs:
        print(
            "Captured server logs and persisted audit metadata passed credential/token/hash redaction checks."
        )
    else:
        print(
            "Persisted audit redaction passed; log-file scanning skipped (no captured local server logs)."
        )
    print(
        "Real HTTP: login, cookies, CSRF/Origin denial, tenant switch, cross-tenant denial, logout/revocation and Phase 0 diagnostics passed."
    )
    print(
        "Synthetic verification credentials remain in backend/.local/http-verification.json (private; not printed)."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:  # noqa: BLE001 - never print HTTP tokens or credentials on failure
        raise SystemExit(
            "HTTP verification failed; inspect server status and safe request logs."
        ) from None
