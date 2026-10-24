import asyncio
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcinst.downloads.errors import DownloadError
from mcinst.downloads.models import DownloadProgress, DownloadResult
from mcinst.downloads.settings import DEFAULT_CONCURRENCY
from mcinst.downloads.transfer import ArtifactDownloader
from mcinst.installation.models import Artifact


@dataclass(frozen=True)
class BatchProgress:
    total_files: int
    completed_files: int
    downloaded_files: int
    reused_files: int
    completed_bytes: int
    transferred_bytes: int
    known_total_bytes: int
    unknown_size_count: int


@dataclass(frozen=True)
class BatchResult:
    files: tuple[DownloadResult, ...]
    transferred_bytes: int


class DownloadScheduler:
    def __init__(self, downloader: ArtifactDownloader, concurrency: int = DEFAULT_CONCURRENCY):
        if type(concurrency) is not int or not 1 <= concurrency <= 64:
            raise ValueError("总并发数必须在 1～64 之间")
        self.downloader = downloader
        self.concurrency = concurrency

    async def run(
        self,
        artifacts: Sequence[Artifact],
        root: Path,
        on_progress: Callable[[BatchProgress], None] | None = None,
    ) -> BatchResult:
        requests = [Artifact.model_validate(a.model_dump()) for a in artifacts]
        paths = [a.path.casefold() for a in requests]
        if len(paths) != len(set(paths)):
            raise DownloadError("下载队列包含重复目标路径")
        queue = deque(enumerate(requests))
        results: list[DownloadResult | None] = [None] * len(requests)
        counters = {"completed": 0, "downloaded": 0, "reused": 0, "bytes": 0, "transferred": 0}
        active_progress: dict[str, tuple[int, int]] = {}
        known_total = sum(a.size for a in requests if a.size is not None)
        unknown_count = sum(a.size is None for a in requests)

        def report() -> None:
            if on_progress:
                on_progress(
                    BatchProgress(
                        total_files=len(requests),
                        completed_files=counters["completed"],
                        downloaded_files=counters["downloaded"],
                        reused_files=counters["reused"],
                        completed_bytes=counters["bytes"],
                        transferred_bytes=counters["transferred"],
                        known_total_bytes=known_total,
                        unknown_size_count=unknown_count,
                    )
                )

        def transfer_progress(event: DownloadProgress) -> None:
            previous_attempt, previous_bytes = active_progress.get(event.path, (0, 0))
            before = previous_bytes if previous_attempt == event.attempt else 0
            counters["transferred"] += max(0, event.downloaded_bytes - before)
            active_progress[event.path] = (event.attempt, event.downloaded_bytes)
            report()

        async def worker() -> None:
            while queue:
                index, artifact = queue.popleft()
                result = await self.downloader.download(
                    artifact, root, on_progress=transfer_progress
                )
                results[index] = result
                active_progress.pop(artifact.path, None)
                counters["completed"] += 1
                counters[result.status] += 1
                counters["bytes"] += result.size
                report()

        report()
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(min(self.concurrency, len(requests))):
                    group.create_task(worker())
        except ExceptionGroup as exc:
            # All siblings have finished cancellation/cleanup before leaving TaskGroup.
            first = exc.exceptions[0]
            while isinstance(first, ExceptionGroup):
                first = first.exceptions[0]
            raise first from exc
        return BatchResult(
            files=tuple(result for result in results if result is not None),
            transferred_bytes=counters["transferred"],
        )
