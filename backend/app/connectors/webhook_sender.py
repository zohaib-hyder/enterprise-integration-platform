"""Explicit local-only synthetic sender; importing this module never sends requests."""

import argparse
import getpass
import json
import os
import re
import time
import uuid
from urllib.parse import urlsplit

import httpx

from app.core.webhook_security import signature
from app.schemas.webhook import BusinessEvent, EventType, Source


def event_bytes(source: Source, event_type: EventType, event_id: str) -> bytes:
    data = (
        {
            "name": "Synthetic Webhook Candidate",
            "email": "webhook-candidate@example.test",
            "stage": "NEW",
            "skills": ["Python"],
        }
        if event_type.startswith("candidate.")
        else {
            "title": "Synthetic Webhook Requisition",
            "department": "Technology",
            "location": "Toronto",
            "status": "OPEN",
        }
    )
    event = BusinessEvent(
        schema_version=1,
        source=source,
        event_id=event_id,
        event_type=event_type,
        occurred_at="2030-01-15T15:00:00Z",
        external_id="synthetic-webhook-record-1",
        data=data,
    )
    return json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()


def signed_request(
    secret: str | bytes, body: bytes, scenario: str = "valid"
) -> tuple[bytes, dict[str, str]]:
    timestamp = str(int(time.time()) - (3600 if scenario == "stale" else 0))
    digest = signature(secret, timestamp, body)
    if scenario == "invalid_signature":
        digest = digest[:-1] + ("0" if digest[-1] != "0" else "1")
    if scenario == "altered_payload":
        body = body + b" "  # Semantically valid JSON, different exact signed bytes.
    return body, {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": timestamp,
        "X-Webhook-Signature": digest,
    }


def local_target(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.port != 8000
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not re.fullmatch(r"/api/v1/webhooks/ingest/[A-Za-z0-9_-]{43}", parsed.path)
        ):
            raise ValueError
        # Numeric loopback pins the destination; no user-controlled DNS or redirects.
        return "http://127.0.0.1:8000" + parsed.path
    except ValueError:
        raise ValueError(
            "Only the explicit localhost:8000 webhook ingress allowlist is permitted"
        ) from None


def deliver(target: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
    target = local_target(target)
    with (
        httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client,
        client.stream("POST", target, content=body, headers=headers) as response,
    ):
        content = bytearray()
        for chunk in response.iter_bytes():
            content.extend(chunk)
            if len(content) > 8192:
                raise ValueError("Local response exceeded limit")
        result = json.loads(content)
        # Fixed acknowledgement values prevent a misconfigured local server echoing secrets.
        safe = {}
        if not isinstance(result, dict):
            raise TypeError("Invalid local acknowledgement")
        if result.get("status") in {"accepted", "duplicate"}:
            safe["status"] = result["status"]
        if result.get("processing_status") == "PENDING":
            safe["processing_status"] = "PENDING"
        from app.core.webhook_security import IngestionCategory

        if result.get("category") in {value.value for value in IngestionCategory}:
            safe["category"] = result["category"]
        for field in ("event_id", "correlation_id"):
            if field in result:
                safe[field] = str(uuid.UUID(result[field]))
        return response.status_code, safe


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicit synthetic localhost webhook delivery")
    parser.add_argument("--target", required=True)
    parser.add_argument("--source", choices=["synthetic_crm", "synthetic_ats"], required=True)
    parser.add_argument(
        "--event-type",
        choices=["job.created", "job.updated", "candidate.created", "candidate.updated"],
        default="job.created",
    )
    parser.add_argument("--event-id", required=True)
    parser.add_argument(
        "--scenario",
        choices=["valid", "duplicate", "invalid_signature", "stale", "altered_payload"],
        default="valid",
    )
    args = parser.parse_args()
    try:
        target = local_target(args.target)
        secret = os.environ.get("WEBHOOK_SIGNING_SECRET") or getpass.getpass(
            "One-time signing secret (hidden): "
        )
        body = event_bytes(args.source, args.event_type, args.event_id)
        body, headers = signed_request(secret, body, args.scenario)
        for _ in range(2 if args.scenario == "duplicate" else 1):
            status, result = deliver(target, body, headers)
            print(json.dumps({"http_status": status, **result}))
    except Exception:  # noqa: BLE001 - never print supplied credentials or raw HTTP exceptions
        raise SystemExit(
            "Synthetic delivery failed; verify allowlisted target, schema and local server."
        ) from None


if __name__ == "__main__":
    main()
