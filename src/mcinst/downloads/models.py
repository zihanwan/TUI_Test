from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class AttemptFailure:
    source: str
    attempt: int
    reason: str
    retryable: bool
    status_code: int | None = None


@dataclass(frozen=True)
class DownloadProgress:
    path: str
    source: str
    attempt: int
    downloaded_bytes: int
    total_bytes: int | None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    status: Literal["downloaded", "reused"]
    source: str | None
    size: int
    sha1: str
    verification: Literal["sha1", "size", "none"]
    attempts: int
    failures: tuple[AttemptFailure, ...] = ()
