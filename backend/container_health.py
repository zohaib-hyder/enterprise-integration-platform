"""Container liveness probes; never print connection URLs or exception details."""

import os
import sys
import urllib.request
import uuid
from datetime import timedelta
from pathlib import Path

if len(sys.argv) > 1 and sys.argv[1] == "worker":
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app.core.config import Settings
    from app.core.security import utcnow
    from app.db.session import build_engine
    from app.models.workflow import WorkerLease

    try:
        worker_id = uuid.UUID(Path(os.environ["WORKER_HEALTH_FILE"]).read_text().strip())
    except (KeyError, ValueError, OSError):
        raise SystemExit(1) from None
    engine = build_engine(Settings())
    if engine is None:
        raise SystemExit(1)
    try:
        with Session(engine) as db:
            healthy = db.scalar(
                select(WorkerLease.id)
                .where(
                    WorkerLease.id == worker_id,
                    WorkerLease.stopped_at.is_(None),
                    WorkerLease.heartbeat_at > utcnow() - timedelta(seconds=60),
                )
                .limit(1)
            )
        raise SystemExit(0 if healthy else 1)
    finally:
        engine.dispose()
else:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/ready", timeout=4) as response:
            raise SystemExit(0 if response.status == 200 else 1)
    except (OSError, ValueError):
        raise SystemExit(1) from None
