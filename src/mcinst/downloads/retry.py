import math
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit():
        try:
            return float(value)
        except (OverflowError, ValueError):
            return math.inf
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            return None
        return max(0.0, (when - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class RetryPolicy:
    attempts_per_source: int = 2
    initial_delay: float = 0.5
    max_delay: float = 10.0

    def __post_init__(self) -> None:
        if type(self.attempts_per_source) is not int or not 1 <= self.attempts_per_source <= 10:
            raise ValueError("每来源尝试次数必须在 1～10 之间")
        if (
            not math.isfinite(self.initial_delay)
            or not math.isfinite(self.max_delay)
            or not 0 <= self.initial_delay <= self.max_delay <= 60
        ):
            raise ValueError("退避时间必须满足 0 <= initial_delay <= max_delay <= 60")

    def delay(self, source_attempt: int, retry_after: str | None = None) -> float | None:
        requested = retry_after_seconds(retry_after)
        # Do not retry earlier than the server requests. Move to another source
        # when its wait exceeds this transfer's budget, rather than truncating it.
        if requested is not None and requested > self.max_delay:
            return None
        backoff = min(self.max_delay, self.initial_delay * 2 ** (source_attempt - 1))
        return max(backoff, requested or 0.0)
