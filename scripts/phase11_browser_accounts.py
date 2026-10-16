"""Retained fresh synthetic principals for each bounded E2E run; no limit bypass."""

import json
import uuid
from pathlib import Path

from app.core.security import password_hasher
from app.main import app  # noqa:F401
from app.models.identity import Membership, User
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

root = Path(__file__).resolve().parents[1]
p = root / "backend/.local/phase11-fixtures.json"
f = json.loads(p.read_text())
engine = create_engine(f["database_url"])
with Session(engine) as db, db.begin():
    suffix = uuid.uuid4().hex[:10]
    for key, role in [
        ("email", "TENANT_ADMIN"),
        ("operations_email", "OPERATIONS_USER"),
    ]:
        u = User(
            email=f"phase11-{key}-{suffix}@example.test",
            display_name="Phase 11 synthetic operator"
            if key == "email"
            else "Phase 11 Operations",
            password_hash=password_hasher.hash(f["password"]),
        )
        db.add(u)
        db.flush()
        db.add(Membership(user_id=u.id, tenant_id=uuid.UUID(f["alpha"]), role=role))
        if key == "email":
            db.add(Membership(user_id=u.id, tenant_id=uuid.UUID(f["beta"]), role=role))
        f[key] = u.email
p.write_text(json.dumps(f, indent=2))
p.chmod(0o600)
engine.dispose()
print("Created new retained synthetic browser principals; login limits unchanged.")
