import asyncio
import hashlib
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import httpx

from mcinst.downloads import ArtifactDownloader, RetryPolicy
from mcinst.downloads.errors import DownloadError, DownloadFailed
from mcinst.downloads.limits import SourceLimiter
from mcinst.downloads.scheduler import DownloadScheduler
from mcinst.installation.models import Artifact
from mcinst.sources import SourceCandidate

BODY = b"payload"


def artifact(index, source="one"):
    return Artifact(
        kind="library",
        path=f"libraries/{index}.jar",
        upstream_url=f"https://{source}.example/{index}",
        candidates=[SourceCandidate(source, f"https://{source}.example/{index}")],
        sha1=hashlib.sha1(BODY).hexdigest(),
        size=len(BODY),
    )


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    async def test_global_and_per_source_limits_and_result_order(self):
        active = Counter()
        peaks = Counter()
        global_peak = 0

        class Stream(httpx.AsyncByteStream):
            def __init__(self, host):
                self.host = host

            async def __aiter__(self):
                nonlocal global_peak
                active[self.host] += 1
                peaks[self.host] = max(peaks[self.host], active[self.host])
                global_peak = max(global_peak, sum(active.values()))
                try:
                    await asyncio.sleep(0.005)
                    yield BODY
                finally:
                    active[self.host] -= 1

        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Stream(request.url.host))
        )
        requests = [artifact(i, "one" if i % 2 else "two") for i in range(12)]
        events = []
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = ArtifactDownloader(client, limiter=SourceLimiter(2))
            result = await DownloadScheduler(downloader, concurrency=3).run(
                requests, self.root, events.append
            )
        self.assertLessEqual(global_peak, 3)
        self.assertGreaterEqual(global_peak, 2)
        self.assertTrue(all(peak <= 2 for peak in peaks.values()))
        self.assertEqual([r.path.name for r in result.files], [f"{i}.jar" for i in range(12)])
        self.assertEqual(events[-1].completed_files, 12)
        self.assertEqual(events[-1].downloaded_files, 12)
        self.assertEqual(events[-1].transferred_bytes, len(BODY) * 12)

    async def test_failure_cancels_peers_and_cleans_staging(self):
        started = asyncio.Event()
        closed = asyncio.Event()

        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield BODY

            async def aclose(self):
                closed.set()

        async def handle(request):
            if request.url.path == "/0":
                await asyncio.wait_for(started.wait(), 2)
                return httpx.Response(404)
            return httpx.Response(200, stream=Waiting())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            downloader = ArtifactDownloader(client, retry=RetryPolicy(attempts_per_source=1))
            with self.assertRaises(DownloadFailed):
                await DownloadScheduler(downloader, 2).run([artifact(0), artifact(1)], self.root)
            self.assertEqual(downloader._active, set())
        self.assertTrue(closed.is_set())
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_retry_backoff_does_not_hold_source_slot(self):
        sleeping, release = asyncio.Event(), asyncio.Event()
        visits = []

        async def sleep(delay):
            sleeping.set()
            await release.wait()

        def handle(request):
            visits.append(request.url.path)
            if request.url.path == "/0" and visits.count("/0") == 1:
                return httpx.Response(502)
            release.set()
            return httpx.Response(200, content=BODY)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            downloader = ArtifactDownloader(client, limiter=SourceLimiter(1), sleep=sleep)
            result = await asyncio.wait_for(
                DownloadScheduler(downloader, 2).run([artifact(0), artifact(1)], self.root),
                3,
            )
        self.assertTrue(sleeping.is_set())
        self.assertEqual(len(result.files), 2)

    async def test_duplicate_paths_fail_before_any_download(self):
        transport = httpx.MockTransport(lambda _: self.fail("重复路径不能进入下载"))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(DownloadError):
                await DownloadScheduler(ArtifactDownloader(client)).run(
                    [artifact(0), artifact(0)],
                    self.root,
                )
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_empty_queue_completes(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: self.fail())
        ) as client:
            result = await DownloadScheduler(ArtifactDownloader(client)).run([], self.root)
        self.assertEqual(result.files, ())
