"""Generate development-only env files without overwriting existing configuration."""

import base64
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
paths = [root / ".env", root / "backend/.env", root / "frontend/.env.local"]
if any(path.exists() for path in paths):
    raise SystemExit(
        "Configuration already exists; configure missing files manually from examples."
    )
password = secrets.token_urlsafe(32)
contents = [
    f"POSTGRES_USER=integration\nPOSTGRES_PASSWORD={password}\nPOSTGRES_DB=integration\nPOSTGRES_PORT=5432\n",
    f'APP_ENV=development\nDATABASE_URL=postgresql+psycopg://integration:{password}@127.0.0.1:5432/integration\nCORS_ORIGINS=["http://localhost:3000"]\nWEBHOOK_ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}\nCREDENTIAL_ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}\nCSRF_SECRET={secrets.token_urlsafe(48)}\n',
    "BACKEND_URL=http://127.0.0.1:8000\nFRONTEND_ORIGINS=http://localhost:3000,http://127.0.0.1:3000\n",
]
for path, content in zip(paths, contents):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)
print("Created private local environment files. Credentials were not printed.")
