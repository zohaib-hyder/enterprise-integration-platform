"""Single-slot foreground worker. No external I/O or waiting inside transactions."""

import argparse
import logging
import os
import signal
import threading
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import utcnow
from app.db.session import build_engine
from app.models.workflow import WorkerLease
from app.services import notifications
from app.workflows.engine import claim, dispatch, register_worker, run_claim


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--lease-seconds", type=int, default=30)
    args = parser.parse_args()
    if not 0.1 <= args.poll_seconds <= 5 or not 15 <= args.lease_seconds <= 120:
        parser.error("Poll must be 0.1–5 seconds and lease 15–120 seconds")
    engine = build_engine(Settings())
    if engine is None:
        parser.error("Configure DATABASE_URL before running the worker")
    logger = logging.getLogger(__name__)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with Session(engine) as db, db.begin():
        worker_id = register_worker(db)
    if path := os.environ.get("WORKER_HEALTH_FILE"):
        marker = Path(path)
        marker.write_text(str(worker_id))
        marker.chmod(0o600)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info("worker_started worker_id=%s", worker_id)
    try:
        while not stop.is_set():
            try:
                with Session(engine) as db, db.begin():
                    dispatch(db)
                with Session(engine) as db, db.begin():
                    job = claim(db, worker_id, args.lease_seconds)
                if job:
                    run_claim(engine, job)
                with Session(engine) as db, db.begin():
                    action = notifications.claim(db, worker_id, args.lease_seconds)
                if action:
                    notifications.run_claim(engine, action)
                if not job and not action:
                    stop.wait(args.poll_seconds)
            except Exception:  # noqa: BLE001 - worker must sanitize failures and recover
                logger.error("worker_iteration_failed category=DATABASE_OR_ENGINE_ERROR")
                stop.wait(args.poll_seconds)
    finally:
        with Session(engine) as db, db.begin():
            worker = db.get(WorkerLease, worker_id)
            worker.stopped_at = worker.heartbeat_at = utcnow()
        engine.dispose()
        logger.info("worker_stopped worker_id=%s", worker_id)


if __name__ == "__main__":
    main()
