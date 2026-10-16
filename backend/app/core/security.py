import hashlib
import hmac
import secrets
from datetime import UTC, datetime

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import HTTPException, Request, Response

# Argon2id: modest memory footprint for an 8GB development machine.
password_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
DUMMY_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def utcnow() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_password(encoded: str, password: str) -> bool:
    try:
        return password_hasher.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def cookie_names(request: Request) -> tuple[str, str]:
    prefix = "__Host-" if request.app.state.settings.app_env == "production" else ""
    return prefix + "platform_session", prefix + "platform_csrf"


def set_cookie(request: Request, response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=request.app.state.settings.app_env == "production",
        samesite="strict",
        path="/",
    )


def csrf_signature(request: Request, value: str) -> str:
    session_name, _ = cookie_names(request)
    binding = token_hash(request.cookies.get(session_name, "anonymous"))
    return hmac.new(
        request.app.state.csrf_secret, f"{binding}:{value}".encode(), hashlib.sha256
    ).hexdigest()


def csrf_valid(request: Request, token: str) -> bool:
    try:
        nonce, issued, signature = token.split(".")
        age = int(utcnow().timestamp()) - int(issued)
        return (
            len(nonce) == 43
            and 0 <= age < 3600
            and hmac.compare_digest(signature, csrf_signature(request, f"{nonce}.{issued}"))
        )
    except (ValueError, TypeError):
        return False


def issue_csrf(request: Request, response: Response) -> str:
    _, name = cookie_names(request)
    token = request.cookies.get(name, "")
    if not csrf_valid(request, token):
        value = f"{secrets.token_urlsafe(32)}.{int(utcnow().timestamp())}"
        token = f"{value}.{csrf_signature(request, value)}"
    set_cookie(request, response, name, token, 3600)
    response.headers["Cache-Control"] = "no-store"
    return token


def require_csrf(request: Request) -> None:
    # Never trust Forwarded/X-Forwarded-Origin headers. BFF forwards the browser Origin unchanged.
    if request.headers.get("origin") not in request.app.state.settings.auth_origins:
        raise HTTPException(403, "Request verification failed")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Request verification failed")
    _, name = cookie_names(request)
    cookie = request.cookies.get(name, "")
    header = request.headers.get("x-csrf-token", "")
    if (
        not cookie
        or not cookie.isascii()
        or not header.isascii()
        or not header
        or len(header) > 256
        or not hmac.compare_digest(cookie, header)
        or not csrf_valid(request, header)
    ):
        raise HTTPException(403, "Request verification failed")
