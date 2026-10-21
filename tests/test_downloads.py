import asyncio
import gzip
import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from mcinst.downloads import ArtifactDownloader, RetryPolicy
from mcinst.downloads.errors import DownloadBusy, DownloadError, DownloadFailed
from mcinst.downloads.retry import retry_after_seconds
from mcinst.installation.models import Artifact
from mcinst.sources import SourceCandidate

CONTENT = b"a small Minecraft artifact\n"
CHECKSUM = hashlib.sha1(CONTENT).hexdigest()
CONTENT_SIZE = len(CONTENT)


def artifact(*, sha1=CHECKSUM, size=CONTENT_SIZE, sources=("mirror", "official")):
    return Artifact(
        kind="client",
        path="versions/test/test.jar",
        upstream_url="https://official.example/client.jar",
        candidates=[
            SourceCandidate(name, f"https://{name}.example/client.jar") for name in sources
        ],
        sha1=sha1,
        size=size,
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "versions/test/test.jar"
        self.sleep = AsyncMock()
        self.addCleanup(self.temporary.cleanup)

    def downloader(self, client, **options):
        return ArtifactDownloader(client, sleep=self.sleep, chunk_size=4, **options)

    def write_existing(self, content=CONTENT):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(content)

    def assert_no_staging(self):
        self.assertEqual(list(self.root.rglob("*.part")), [])

    async def test_streamed_success_commits_only_after_verification(self):
        chunks = Chunks([CONTENT[:8], CONTENT[8:]])
        progress = []

        def observe(event):
            self.assertFalse(self.target.exists())
            progress.append(event)

        def handle(request):
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            self.assertNotIn("Range", request.headers)
            return httpx.Response(200, stream=chunks)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(
                artifact(), self.root, on_progress=observe
            )
        self.assertEqual(self.target.read_bytes(), CONTENT)
        self.assertEqual(result.sha1, CHECKSUM)
        self.assertEqual(result.verification, "sha1")
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(progress[0].downloaded_bytes, 0)
        self.assertEqual(progress[-1].downloaded_bytes, len(CONTENT))
        self.assertTrue(chunks.closed)
        self.assert_no_staging()

    async def test_hash_verified_existing_file_is_reused_without_network(self):
        self.write_existing()

        def unexpected(request):
            self.fail("已验证的文件不应再次下载")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(result.status, "reused")
        self.assertEqual(result.attempts, 0)
        self.assertIsNone(result.source)
        self.assert_no_staging()

    async def test_bad_existing_file_is_replaced(self):
        self.write_existing(b"corrupt")
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=CONTENT))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(self.target.read_bytes(), CONTENT)

    async def test_size_only_existing_file_is_not_reused(self):
        self.write_existing(b"x" * len(CONTENT))
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=CONTENT))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await self.downloader(client).download(artifact(sha1=None), self.root)
        self.assertEqual(result.status, "downloaded")
        self.assertEqual(result.verification, "size")
        self.assertEqual(self.target.read_bytes(), CONTENT)

    async def test_unknown_integrity_is_explicit(self):
        stream = Chunks([CONTENT])
        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=stream))
        async with httpx.AsyncClient(transport=transport) as client:
            result = await self.downloader(client).download(
                artifact(sha1=None, size=None), self.root
            )
        self.assertEqual(result.verification, "none")
        self.assertEqual(result.size, len(CONTENT))
        self.assertEqual(result.sha1, CHECKSUM)

    async def test_502_retries_then_falls_back_to_official(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            if request.url.host == "mirror.example":
                return httpx.Response(502)
            return httpx.Response(200, content=CONTENT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(visited, ["mirror.example", "mirror.example", "official.example"])
        self.assertEqual(result.source, "official")
        self.assertEqual(result.attempts, 3)
        self.assertEqual([f.status_code for f in result.failures], [502, 502])
        self.sleep.assert_awaited_once_with(0.5)
        self.assert_no_staging()

    async def test_permanent_404_skips_same_source_retry(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            return (
                httpx.Response(404) if len(visited) == 1 else httpx.Response(200, content=CONTENT)
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(visited, ["mirror.example", "official.example"])
        self.assertFalse(result.failures[0].retryable)
        self.sleep.assert_not_awaited()

    async def test_fixed_source_exhaustion_does_not_invent_a_fallback(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            return httpx.Response(502)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(DownloadFailed) as failure:
                await self.downloader(client).download(artifact(sources=("mirror",)), self.root)
        self.assertEqual(visited, ["mirror.example", "mirror.example"])
        self.assertEqual(len(failure.exception.failures), 2)
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    async def test_corrupt_mirror_falls_back_without_publishing_bad_bytes(self):
        def handle(request):
            body = b"x" * CONTENT_SIZE if request.url.host == "mirror.example" else CONTENT
            return httpx.Response(200, content=body)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(result.source, "official")
        self.assertTrue(all("SHA-1" in f.reason for f in result.failures))
        self.assertEqual(self.target.read_bytes(), CONTENT)
        self.assert_no_staging()

    async def test_partial_network_failure_restarts_from_zero(self):
        broken = Chunks([b"partial!"], httpx.ReadError("connection lost"))
        count = 0
        events = []

        def handle(request):
            nonlocal count
            count += 1
            self.assertNotIn("Range", request.headers)
            if count == 1:
                return httpx.Response(200, stream=broken)
            return httpx.Response(200, content=CONTENT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(
                artifact(), self.root, on_progress=events.append
            )
        self.assertEqual(result.attempts, 2)
        self.assertEqual(self.target.read_bytes(), CONTENT)
        starts = [e.attempt for e in events if e.downloaded_bytes == 0]
        self.assertEqual(starts, [1, 2])
        self.assertTrue(broken.closed)
        self.assert_no_staging()

    async def test_integrity_failure_preserves_old_target(self):
        self.write_existing(b"old-version")
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * len(CONTENT)))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(DownloadFailed) as failure:
                await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(len(failure.exception.failures), 4)
        self.assertTrue(all("SHA-1" in f.reason for f in failure.exception.failures))
        self.assertEqual(self.target.read_bytes(), b"old-version")
        self.assert_no_staging()

    async def test_size_and_partial_response_errors_do_not_publish_files(self):
        for mode in ("short", "long", "wrong_header", "partial_206"):

            def handle(request, mode=mode):
                if mode == "short":
                    return httpx.Response(200, stream=Chunks([CONTENT[:-1]]))
                if mode == "long":
                    return httpx.Response(200, stream=Chunks([CONTENT + b"!"]))
                if mode == "wrong_header":
                    return httpx.Response(200, content=CONTENT[:-1])
                return httpx.Response(206, content=CONTENT)

            with self.subTest(mode=mode):
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    with self.assertRaises(DownloadFailed):
                        await self.downloader(client).download(artifact(), self.root)
                self.assertFalse(self.target.exists())
                self.assert_no_staging()

    async def test_decoded_http_compression_uses_uncompressed_file_size(self):
        compressed = gzip.compress(CONTENT)
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                content=compressed,
                headers={"Content-Encoding": "gzip"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(result.size, len(CONTENT))
        self.assertEqual(self.target.read_bytes(), CONTENT)

    async def test_redirected_cdn_download_is_supported(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            if len(visited) == 1:
                return httpx.Response(302, headers={"Location": "https://cdn.example/content"})
            return httpx.Response(200, content=CONTENT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(visited, ["mirror.example", "cdn.example"])
        self.assertEqual(result.source, "mirror")
        self.assertEqual(result.attempts, 1)

    async def test_zero_length_file(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b""))
        empty_hash = hashlib.sha1(b"").hexdigest()
        async with httpx.AsyncClient(transport=transport) as client:
            result = await self.downloader(client).download(
                artifact(size=0, sha1=empty_hash), self.root
            )
        self.assertEqual(result.size, 0)
        self.assertEqual(self.target.read_bytes(), b"")

    async def test_retry_after_is_respected_or_source_is_skipped(self):
        for wait, expected_hosts, expected_delays in (
            ("2", ["mirror.example", "mirror.example", "official.example"], [2.0]),
            ("120", ["mirror.example", "official.example"], []),
        ):
            visited = []
            self.sleep.reset_mock()

            def handle(request, wait=wait, visited=visited):
                visited.append(request.url.host)
                if request.url.host == "mirror.example":
                    return httpx.Response(429, headers={"Retry-After": wait})
                return httpx.Response(200, content=CONTENT)

            with self.subTest(wait=wait):
                self.target.unlink(missing_ok=True)
                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                    await self.downloader(client).download(artifact(), self.root)
                self.assertEqual(visited, expected_hosts)
                self.assertEqual([c.args[0] for c in self.sleep.await_args_list], expected_delays)

    async def test_cancellation_cleans_staging_and_preserves_target(self):
        self.write_existing(b"old-version")
        started = asyncio.Event()

        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"part"
                started.set()
                await asyncio.Event().wait()

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=Waiting()))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = self.downloader(client)
            task = asyncio.create_task(downloader.download(artifact(), self.root))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.target.read_bytes(), b"old-version")
        self.assert_no_staging()
        self.assertEqual(downloader._active, set())

    async def test_local_disk_error_is_not_retried_as_a_network_error(self):
        self.write_existing(b"keep-original")
        calls = []

        def handle(request):
            calls.append(request.url.host)
            return httpx.Response(200, content=CONTENT)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with patch("mcinst.downloads.transfer.os.replace", side_effect=PermissionError("disk")):
                with self.assertRaisesRegex(DownloadError, "本地文件"):
                    await self.downloader(client).download(artifact(), self.root)
        self.assertEqual(calls, ["mirror.example"])
        self.sleep.assert_not_awaited()
        self.assertEqual(self.target.read_bytes(), b"keep-original")
        self.assert_no_staging()

    async def test_callback_error_propagates_without_retry(self):
        def callback(event):
            raise RuntimeError("UI callback bug")

        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=CONTENT))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(RuntimeError, "callback bug"):
                await self.downloader(client).download(artifact(), self.root, on_progress=callback)
        self.sleep.assert_not_awaited()
        self.assertFalse(self.target.exists())
        self.assert_no_staging()

    async def test_http_error_from_callback_is_not_mistaken_for_source_failure(self):
        def callback(event):
            raise httpx.ConnectError("callback failure")

        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=CONTENT))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaisesRegex(httpx.ConnectError, "callback failure"):
                await self.downloader(client).download(artifact(), self.root, on_progress=callback)
        self.sleep.assert_not_awaited()
        self.assert_no_staging()

    async def test_mutated_paths_and_invalid_sources_fail_before_writing(self):
        for field, value in (
            ("path", "../escape.jar"),
            ("candidates", []),
            ("candidates", [SourceCandidate("bad", "http://example.com/x")]),
        ):
            request = artifact()
            setattr(request, field, value)
            with self.subTest(field=field, value=value):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda _: self.fail())
                ) as client:
                    with self.assertRaises(DownloadError):
                        await self.downloader(client).download(request, self.root / "new-root")
                self.assertFalse((self.root / "new-root").exists())

    async def test_symlink_target_and_parent_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel.jar"
        sentinel.write_bytes(b"do not modify")
        self.target.parent.mkdir(parents=True)
        self.target.symlink_to(sentinel)
        transport = httpx.MockTransport(lambda _: self.fail("符号链接不应发起下载"))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = self.downloader(client)
            with self.assertRaisesRegex(DownloadError, "符号链接"):
                await downloader.download(artifact(), self.root)
            self.target.unlink()
            self.target.parent.rmdir()
            self.target.parent.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(DownloadError, "符号链接"):
                await downloader.download(artifact(), self.root)
        self.assertEqual(sentinel.read_bytes(), b"do not modify")

    async def test_same_target_cannot_be_started_twice_on_one_downloader(self):
        started = asyncio.Event()

        class Waiting(httpx.AsyncByteStream):
            async def __aiter__(self):
                started.set()
                await asyncio.Event().wait()
                yield CONTENT

        transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=Waiting()))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = self.downloader(client)
            first = asyncio.create_task(downloader.download(artifact(), self.root))
            await asyncio.wait_for(started.wait(), timeout=2)
            try:
                with self.assertRaises(DownloadBusy):
                    await downloader.download(artifact(), self.root)
            finally:
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
        self.assert_no_staging()


class RetryTests(unittest.TestCase):
    def test_backoff_is_bounded(self):
        policy = RetryPolicy(initial_delay=1, max_delay=3)
        self.assertEqual([policy.delay(i) for i in (1, 2, 3, 4)], [1, 2, 3, 3])
        self.assertIsNone(policy.delay(1, "4"))
        self.assertEqual(policy.delay(1, "invalid"), 1)

    def test_retry_after_supports_dates_and_invalid_values(self):
        self.assertEqual(retry_after_seconds("2"), 2)
        self.assertEqual(retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT"), 0)
        self.assertIsNone(retry_after_seconds("-1"))
        self.assertIsNone(retry_after_seconds("invalid"))
        with patch("mcinst.downloads.retry.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 21, 7, 28, 0, tzinfo=UTC)
            self.assertEqual(retry_after_seconds("Wed, 21 Oct 2026 07:28:05 GMT"), 5)

    def test_invalid_retry_config_is_rejected(self):
        for options in (
            {"attempts_per_source": 0},
            {"initial_delay": -1},
            {"max_delay": 61},
            {"initial_delay": float("nan")},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                RetryPolicy(**options)
