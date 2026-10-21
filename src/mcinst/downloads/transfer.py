import asyncio
import hashlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from mcinst.downloads.errors import DownloadBusy, DownloadError, DownloadFailed
from mcinst.downloads.files import check_target, existing_match, resolve_root
from mcinst.downloads.limits import SourceLimiter
from mcinst.downloads.models import AttemptFailure, DownloadProgress, DownloadResult
from mcinst.downloads.retry import RETRYABLE_STATUS, RetryPolicy
from mcinst.installation.models import Artifact
from mcinst.sources import SourceCandidate

ProgressCallback = Callable[[DownloadProgress], None]


class _CallbackFailure(Exception):
    def __init__(self, error: Exception):
        self.error = error


class _SourceFailure(Exception):
    def __init__(
        self,
        reason: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        retry_after: str | None = None,
    ):
        super().__init__(reason)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


def _snapshot(artifact: Artifact) -> Artifact:
    try:
        # Revalidate mutable models and isolate the request from later UI changes.
        snapshot = Artifact.model_validate(artifact.model_dump())
        if not snapshot.candidates:
            raise ValueError("没有下载候选来源")
        for candidate in snapshot.candidates:
            parts = urlsplit(candidate.url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise ValueError("下载候选必须是无账号凭据的 HTTPS 地址")
            # Accessing .port validates malformed ports before creating files.
            _ = parts.port
        return snapshot
    except (ValidationError, ValueError) as exc:
        raise DownloadError(f"无效的下载请求：{exc}") from exc


class ArtifactDownloader:
    """Caller owns the HTTP client; this class does not execute an installation plan."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        retry: RetryPolicy | None = None,
        chunk_size: int = 64 * 1024,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limiter: SourceLimiter | None = None,
    ):
        if type(chunk_size) is not int or not 1 <= chunk_size <= 1024 * 1024:
            raise ValueError("chunk_size 必须在 1～1048576 之间")
        self.client = client
        self.retry = retry or RetryPolicy()
        self.chunk_size = chunk_size
        self.sleep = sleep
        self.limiter = limiter
        self._active: set[Path] = set()

    async def download(
        self,
        artifact: Artifact,
        root: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        request = _snapshot(artifact)
        root = await asyncio.to_thread(resolve_root, Path(root))
        target = root / request.path
        if target in self._active:
            raise DownloadBusy(f"该目标已有下载任务：{request.path}")
        self._active.add(target)
        try:
            check_target(root, request.path, create=True)
            match = await asyncio.to_thread(existing_match, target, request)
            if match is not None:
                check_target(root, request.path, create=False)
                return DownloadResult(
                    path=target,
                    status="reused",
                    source=None,
                    size=match[0],
                    sha1=match[1],
                    verification="sha1",
                    attempts=0,
                )
            return await self._transfer(request, root, on_progress)
        except _CallbackFailure as exc:
            raise exc.error from exc
        except OSError as exc:
            raise DownloadError(
                f"本地文件操作失败：{request.path}（{exc.strerror or exc}）"
            ) from exc
        finally:
            self._active.remove(target)

    async def _transfer(
        self, artifact: Artifact, root: Path, progress: ProgressCallback | None
    ) -> DownloadResult:
        failures: list[AttemptFailure] = []
        attempt = 0
        for candidate in artifact.candidates:
            for source_attempt in range(1, self.retry.attempts_per_source + 1):
                attempt += 1
                try:
                    if self.limiter is None:
                        size, digest = await self._attempt(
                            artifact, root, candidate, attempt, progress
                        )
                    else:
                        async with self.limiter.slot(candidate.name):
                            size, digest = await self._attempt(
                                artifact, root, candidate, attempt, progress
                            )
                    return DownloadResult(
                        path=root / artifact.path,
                        status="downloaded",
                        source=candidate.name,
                        size=size,
                        sha1=digest,
                        verification="sha1"
                        if artifact.sha1
                        else "size"
                        if artifact.size is not None
                        else "none",
                        attempts=attempt,
                        failures=tuple(failures),
                    )
                except _SourceFailure as exc:
                    failure = exc
                except httpx.HTTPError as exc:
                    failure = _SourceFailure(
                        f"网络传输失败（{type(exc).__name__}）",
                        retryable=isinstance(exc, (httpx.TransportError, httpx.DecodingError)),
                    )
                failures.append(
                    AttemptFailure(
                        source=candidate.name,
                        attempt=attempt,
                        reason=str(failure),
                        retryable=failure.retryable,
                        status_code=failure.status_code,
                    )
                )
                if not failure.retryable or source_attempt == self.retry.attempts_per_source:
                    break
                delay = self.retry.delay(source_attempt, failure.retry_after)
                if delay is None:
                    break
                await self.sleep(delay)
        raise DownloadFailed(artifact.path, tuple(failures))

    async def _attempt(
        self,
        artifact: Artifact,
        root: Path,
        candidate: SourceCandidate,
        attempt: int,
        progress: ProgressCallback | None,
    ) -> tuple[int, str]:
        target = check_target(root, artifact.path, create=False)
        descriptor, filename = tempfile.mkstemp(
            prefix=".mcinst-",
            suffix=".part",
            dir=target.parent,
        )
        staging = Path(filename)
        try:
            with os.fdopen(descriptor, "wb") as output:
                async with self.client.stream(
                    "GET",
                    candidate.url,
                    headers={"Accept-Encoding": "identity"},
                    follow_redirects=True,
                ) as response:
                    if response.status_code != 200:
                        raise _SourceFailure(
                            f"HTTP {response.status_code}",
                            retryable=response.status_code in RETRYABLE_STATUS,
                            status_code=response.status_code,
                            retry_after=response.headers.get("Retry-After"),
                        )
                    if response.url.scheme != "https":
                        raise _SourceFailure("重定向到了非 HTTPS 地址", retryable=False)
                    length = self._content_length(response)
                    if length is not None and artifact.size is not None and length != artifact.size:
                        raise _SourceFailure("Content-Length 与计划大小不一致")
                    total = artifact.size if artifact.size is not None else length
                    size = 0
                    digest = hashlib.sha1(usedforsecurity=False)

                    def report() -> None:
                        if progress is not None:
                            try:
                                progress(
                                    DownloadProgress(
                                        path=artifact.path,
                                        source=candidate.name,
                                        attempt=attempt,
                                        downloaded_bytes=size,
                                        total_bytes=total,
                                    )
                                )
                            except Exception as exc:
                                raise _CallbackFailure(exc) from exc

                    report()
                    async for block in response.aiter_bytes(chunk_size=self.chunk_size):
                        size += len(block)
                        if total is not None and size > total:
                            raise _SourceFailure("响应内容超过预期大小")
                        output.write(block)
                        digest.update(block)
                        report()
                if total is not None and size != total:
                    raise _SourceFailure("下载文件大小不匹配")
                checksum = digest.hexdigest()
                if artifact.sha1 is not None and checksum != artifact.sha1:
                    raise _SourceFailure("下载文件 SHA-1 不匹配")
                output.flush()
                os.fsync(output.fileno())
            # No await between the last path check and publication.
            check_target(root, artifact.path, create=False)
            os.replace(staging, target)
            return size, checksum
        finally:
            # Cleanup is a short, synchronous operation so cancellation cannot interrupt it.
            staging.unlink(missing_ok=True)  # noqa: ASYNC240

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        # aiter_bytes decodes HTTP compression; compressed length is not file size.
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            return None
        value = response.headers.get("Content-Length")
        if value is None:
            return None
        if not value.isascii() or not value.isdigit():
            raise _SourceFailure("无效的 Content-Length")
        try:
            return int(value)
        except ValueError as exc:
            raise _SourceFailure("无效的 Content-Length") from exc
