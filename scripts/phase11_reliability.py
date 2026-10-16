"""Reuse real reliability and SIGKILL demos against only the isolated schema."""

import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

root = Path(__file__).resolve().parents[1]
path = root / "backend/.local/phase11-fixtures.json"
f = json.loads(path.read_text())
env = {
    **os.environ,
    "DATABASE_URL": f["database_url"],
    "PYTHONPATH": ".",
    "AI_OPENAI_ENABLED": "false",
    "PLATFORM_VERIFICATION_CREDENTIALS": str(path),
}
for key in ("WEBHOOK_ENCRYPTION_KEY", "CREDENTIAL_ENCRYPTION_KEY"):
    if f.get(key.lower()):
        env[key] = f[key.lower()]
results = []
for name in ("verify_phase6_http.py", "verify_phase8_http.py"):
    output = root / "backend/.local" / ("phase11-" + name + ".log")
    with output.open("w") as log:
        run = subprocess.run(
            [sys.executable, str(root / "scripts" / name)],
            cwd=root / "backend",
            env=env,
            stdout=log,
            stderr=log,
            timeout=300,
            check=False,
        )
    results.append(
        {
            "scenario": name,
            "returncode": run.returncode,
            "output": str(output.relative_to(root)),
        }
    )
    print(name, "PASS" if run.returncode == 0 else "FAIL", flush=True)
# Terminate only a connection created by this probe; never stop the shared database.
engine = create_engine(
    f["database_url"], pool_pre_ping=True, pool_size=1, max_overflow=0
)
with engine.connect() as conn:
    pid = conn.scalar(text("select pg_backend_pid()"))
admin = create_engine(f["database_url"])
with admin.connect() as conn:
    assert conn.scalar(text("select pg_terminate_backend(:pid)"), {"pid": pid})
with engine.connect() as conn:
    assert conn.scalar(text("select 1")) == 1
results.append(
    {"scenario": "owned_connection_interruption_pre_ping_reconnect", "returncode": 0}
)
engine.dispose()
admin.dispose()
(root / "docs/evidence/phase11-reliability.json").write_text(
    json.dumps(results, indent=2)
)
raise SystemExit(0 if all(r["returncode"] == 0 for r in results) else 1)
