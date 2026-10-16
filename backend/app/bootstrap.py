"""Local operator-only bootstrap. No HTTP admin registration exists."""

import getpass
import os
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import password_hasher, verify_password
from app.db.session import build_engine
from app.models.identity import Membership, User
from app.models.tenant import Tenant
from app.schemas.identity import EmailInput
from app.services.audit import record


def bootstrap(
    db: Session, email: str, password: str, display_name: str, tenant_name: str, slug: str
) -> None:
    email = EmailInput(email=email).email
    if (
        len(password) < 12
        or len(password) > 128
        or not display_name.strip()
        or len(display_name) > 120
    ):
        raise ValueError("A display name and a 12–128 character password are required")
    if (
        not tenant_name.strip()
        or len(tenant_name) > 200
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug)
        or len(slug) > 100
    ):
        raise ValueError("A tenant name and normalized slug are required")
    tenant = db.scalar(select(Tenant).where(Tenant.slug == slug).with_for_update())
    if tenant is None:
        tenant = Tenant(name=tenant_name.strip(), slug=slug)
        db.add(tenant)
        db.flush()
    user = db.scalar(select(User).where(User.email == email))
    if user is not None:
        if not user.is_active or not verify_password(user.password_hash, password):
            raise ValueError("Existing account verification failed")
    else:
        user = User(
            email=email,
            password_hash=password_hasher.hash(password),
            display_name=display_name.strip(),
        )
        db.add(user)
        db.flush()
    membership = db.scalar(
        select(Membership).where(Membership.tenant_id == tenant.id, Membership.user_id == user.id)
    )
    if membership is None:
        membership = Membership(
            tenant_id=tenant.id, user_id=user.id, role="TENANT_ADMIN", status="ACTIVE"
        )
        db.add(membership)
        db.flush()
        record(
            db,
            "tenant.bootstrapped",
            "membership",
            membership.id,
            tenant.id,
            user.id,
            {"role": "TENANT_ADMIN"},
        )
    elif membership.role != "TENANT_ADMIN" or membership.status != "ACTIVE":
        raise ValueError("Bootstrap will not elevate an existing membership")


def main() -> None:
    settings = Settings()
    engine = build_engine(settings)
    if engine is None:
        raise SystemExit("DATABASE_URL is required")
    try:
        email = os.environ.get("BOOTSTRAP_EMAIL") or input("Synthetic administrator email: ")
        password = os.environ.get("BOOTSTRAP_PASSWORD") or getpass.getpass(
            "Administrator password (12+ characters): "
        )
        if (
            not os.environ.get("BOOTSTRAP_PASSWORD")
            and getpass.getpass("Confirm password: ") != password
        ):
            raise ValueError("Passwords did not match")
        name = os.environ.get("BOOTSTRAP_DISPLAY_NAME") or input("Display name: ")
        tenant = os.environ.get("BOOTSTRAP_TENANT_NAME") or input("Synthetic organization name: ")
        slug = os.environ.get("BOOTSTRAP_TENANT_SLUG") or input("Organization slug: ")
        with Session(engine) as db, db.begin():
            bootstrap(db, email, password, name, tenant, slug)
        print("Synthetic organization administrator ready. No credentials were printed.")
    except Exception:  # noqa: BLE001 - CLI must not print database URLs or credentials
        raise SystemExit(
            "Bootstrap failed; check inputs and migrated database configuration."
        ) from None
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
