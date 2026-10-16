"""Add a dedicated private local webhook master key without replacing configuration."""

import base64
import os
from pathlib import Path

path = Path(__file__).resolve().parents[1] / "backend/.env"
if not path.is_file():
    raise SystemExit("Configure backend/.env first; no file was changed.")
if any(
    line.strip().startswith("WEBHOOK_ENCRYPTION_KEY=")
    for line in path.read_text().splitlines()
):
    raise SystemExit("Webhook encryption key already configured; no key was changed.")
with path.open("a") as file:
    file.write(
        "\nWEBHOOK_ENCRYPTION_KEY="
        + base64.urlsafe_b64encode(os.urandom(32)).decode()
        + "\n"
    )
path.chmod(0o600)
print(
    "Added a dedicated webhook encryption key to private backend/.env; key was not printed."
)
