"""Configure a dedicated repository-local credential key; never print or replace secrets."""

import base64
import os
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "backend/.env"
if not path.is_file():
    raise SystemExit("Configure backend/.env first; nothing changed.")
if any(
    line.strip().startswith("CREDENTIAL_ENCRYPTION_KEY=")
    for line in path.read_text().splitlines()
):
    raise SystemExit("Credential key is already configured; nothing changed.")
with path.open("a") as file:
    file.write(
        "\nCREDENTIAL_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(os.urandom(32)).decode()
        + "\n"
    )
path.chmod(0o600)
print(
    "Added a dedicated credential key to private backend/.env; no secret was printed."
)
