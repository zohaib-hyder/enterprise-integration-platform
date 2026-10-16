"""Append a fresh real synthetic retry after benchmarks; never reset old delivery state."""

import json
import os
import sys
from pathlib import Path

from app.main import app  # noqa:F401
from app.models.notification import NotificationDelivery
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

root = Path(__file__).resolve().parents[1]
p = root / "backend/.local/phase11-fixtures.json"
f = json.loads(p.read_text())
for key in ("WEBHOOK_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEY"):
    if f.get(key.lower()):
        os.environ[key] = f[key.lower()]
engine = create_engine(f["database_url"])
sys.path.insert(0, str(root / "backend/tests"))
from test_notifications import configured, fault, manual, tick

ids = configured(engine)
identifier = manual(engine, ids)
fault(engine, ids[2], "HTTP_503", retry_after="3600")
for _ in range(30):
    tick(engine, ids[3])
    with Session(engine) as db:
        row = db.get(NotificationDelivery, identifier)
        if row.status == "RETRY_SCHEDULED":
            break
else:
    raise RuntimeError("Could not prepare isolated retry fixture through normal queue")
f["retry_delivery"] = str(identifier)
p.write_text(json.dumps(f, indent=2))
p.chmod(0o600)
engine.dispose()
print("Appended a new real scheduled-retry fixture; prior states retained.")
