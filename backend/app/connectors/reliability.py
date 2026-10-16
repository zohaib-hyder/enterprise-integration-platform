"""One-attempt reliability error contract and pure bounded Retry-After parser."""

import math
import uuid
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from app.connectors.contracts import ConnectorError, ErrorCategory
from app.core.security import utcnow
from app.schemas.workflow import RetryPolicy


def parse_retry_after(value: str | None, now: datetime | None = None, cap: int = 300) -> int | None:
    if value is None or len(value) > 64:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        return min(cap, int(value))
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            return None
        seconds = math.ceil((target.astimezone(UTC) - (now or utcnow())).total_seconds())
        return min(cap, max(0, seconds))
    except (ValueError, TypeError, OverflowError):
        return None


class ReliabilityError(ConnectorError):
    def __init__(
        self,
        category: ErrorCategory,
        reason: str,
        *,
        retryable: bool | None = None,
        retry_after: int | None = None,
        operation_id: uuid.UUID | None = None,
        retry_policy: RetryPolicy | None = None,
    ):
        super().__init__(category)
        if retryable is not None:
            self.retryable = retryable
        self.reason, self.retry_after, self.operation_id = reason, retry_after, operation_id
        self.retry_policy = retry_policy
        self.defer_until = utcnow() + timedelta(seconds=retry_after) if retry_after else None
