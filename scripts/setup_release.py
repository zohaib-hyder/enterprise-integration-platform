"""Generate private isolated rehearsal config; never reuse the development database."""

import argparse
import base64
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default=".local/release")
    args = parser.parse_args()
    directory = (ROOT / args.directory).resolve()
    if not directory.is_relative_to(ROOT / ".local"):
        parser.error("Release fixtures must remain under repository .local")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "release.env"
    password = secrets.token_urlsafe(32)
    values = {
        "RELEASE_ENV_FILE": str(path),
        "POSTGRES_USER": "rehearsal",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": "rehearsal",
        "DATABASE_URL": f"postgresql+psycopg://rehearsal:{password}@postgres:5432/rehearsal",
        # Deliberate test-only mode: local HTTP has no TLS terminator / secure cookies.
        "APP_ENV": "test",
        "APP_DEBUG": "false",
        "CSRF_SECRET": secrets.token_urlsafe(48),
        "WEBHOOK_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "CREDENTIAL_ENCRYPTION_KEY": base64.urlsafe_b64encode(
            secrets.token_bytes(32)
        ).decode(),
        "ALLOWED_HOSTS": '["api","localhost","127.0.0.1"]',
        "AUTH_ORIGINS": '["http://127.0.0.1:3300"]',
        "CORS_ORIGINS": "[]",
        "FRONTEND_ORIGINS": "http://127.0.0.1:3300",
        "AI_OPENAI_ENABLED": "false",
        "BOOTSTRAP_EMAIL": "release-admin@example.test",
        "BOOTSTRAP_PASSWORD": secrets.token_urlsafe(32),
        "BOOTSTRAP_DISPLAY_NAME": "Synthetic release operator",
        "BOOTSTRAP_TENANT_NAME": "Synthetic Release Staffing",
        "BOOTSTRAP_TENANT_SLUG": "synthetic-release-staffing",
    }
    with os.fdopen(
        os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as out:
        out.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
    print("Created private rehearsal configuration; no credentials printed.")


if __name__ == "__main__":
    main()
