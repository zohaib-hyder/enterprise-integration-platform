"""Run the existing API/worker against the retained isolated Phase 11 schema."""

import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
fixture = json.loads((root / "backend/.local/phase11-fixtures.json").read_text())
os.environ["DATABASE_URL"] = fixture["database_url"]
os.environ["AI_OPENAI_ENABLED"] = "false"
for key in ("WEBHOOK_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEY"):
    if fixture.get(key.lower()):
        os.environ[key] = fixture[key.lower()]
os.chdir(root / "backend")
if sys.argv[1] == "api":
    args = [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--no-access-log",
    ]
else:
    args = [sys.executable, "-m", "app.workers.runner", "--poll-seconds", ".2"]
os.execv(sys.executable, args)
