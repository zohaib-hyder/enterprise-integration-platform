"""Fixed HMAC contract and context-bound AES-256-GCM secret storage."""

import base64
import hashlib
import hmac
import os
import re
import secrets
import uuid
from enum import StrEnum
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

MAX_BODY = 32768


class IngestionCategory(StrEnum):
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    STALE_TIMESTAMP = "STALE_TIMESTAMP"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    UNSUPPORTED_CONTENT_TYPE = "UNSUPPORTED_CONTENT_TYPE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    MALFORMED_JSON = "MALFORMED_JSON"
    INVALID_SCHEMA = "INVALID_SCHEMA"
    INVALID_SOURCE = "INVALID_SOURCE"
    CONFLICTING_EVENT = "CONFLICTING_EVENT"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"


class IngestionError(Exception):
    def __init__(self, category: IngestionCategory, status: int):
        self.category, self.status = category, status
        super().__init__("Webhook request rejected")


def decode_key(value: str) -> bytes:
    try:
        decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode() != value:
            raise ValueError
        return decoded
    except (ValueError, TypeError):
        raise ValueError(
            "Webhook encryption key must be canonical URL-safe base64 of 32 bytes"
        ) from None


def encryption(settings: Settings) -> AESGCM:
    if settings.webhook_encryption_key is None:
        raise IngestionError(IngestionCategory.SECRET_UNAVAILABLE, 503)
    return AESGCM(decode_key(settings.webhook_encryption_key.get_secret_value()))


def aad(tenant: uuid.UUID, endpoint: uuid.UUID, version: int) -> bytes:
    return f"northstar:webhook:v1:{tenant}:{endpoint}:{version}".encode()


def seal(
    settings: Settings, tenant: uuid.UUID, endpoint: uuid.UUID, version: int
) -> tuple[str, bytes, bytes]:
    cipher = encryption(settings)
    secret = secrets.token_urlsafe(32)
    nonce = os.urandom(12)
    encrypted = cipher.encrypt(nonce, secret.encode(), aad(tenant, endpoint, version))
    return secret, nonce, encrypted


class EncryptedSecret(Protocol):
    tenant_id: uuid.UUID
    endpoint_id: uuid.UUID
    version: int
    nonce: bytes
    ciphertext: bytes


def unseal(settings: Settings, row: EncryptedSecret) -> bytes:
    try:
        return encryption(settings).decrypt(
            row.nonce, row.ciphertext, aad(row.tenant_id, row.endpoint_id, row.version)
        )
    except (InvalidTag, ValueError):
        raise IngestionError(IngestionCategory.SECRET_UNAVAILABLE, 503) from None


def signature(secret: str | bytes, timestamp: str, body: bytes) -> str:
    key = secret.encode() if isinstance(secret, str) else secret
    return (
        "v1="
        + hmac.new(
            key, b"v1." + timestamp.encode("ascii") + b"." + body, hashlib.sha256
        ).hexdigest()
    )


def signature_matches(secret: bytes, timestamp: str, body: bytes, supplied: str) -> bool:
    if not re.fullmatch(r"v1=[0-9a-f]{64}", supplied):
        return False
    return hmac.compare_digest(signature(secret, timestamp, body), supplied)
