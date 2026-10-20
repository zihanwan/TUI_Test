import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

import httpx

from mcinst.catalog import CatalogService
from mcinst.errors import MetadataError, VersionNotFound
from mcinst.sources import MANIFEST_URL, SourcePolicy, metadata_candidates

DETAIL_URL = "https://piston-meta.mojang.com/v1/packages/example/test.json"
DETAIL = json.dumps(
    {
        "id": "1.20.1",
        "javaVersion": {"component": "java-runtime-gamma", "majorVersion": 17},
        "libraries": [],
        "futureUpstreamField": {"keep": True},
    }
).encode()


def manifest(detail: bytes = DETAIL) -> dict:
    return {
        "latest": {"release": "1.20.1", "snapshot": "sample-snapshot"},
        "versions": [
            {
                "id": "1.20.1",
                "type": "release",
                "url": DETAIL_URL,
                "sha1": hashlib.sha1(detail).hexdigest(),
                "releaseTime": "2023-06-12T12:00:00Z",
                "time": "2023-06-12T12:00:00Z",
            }
        ],
    }


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_fallback_from_mirror_to_official(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            if request.url.host == "bmclapi2.bangbang93.com":
                return httpx.Response(503)
            return httpx.Response(200, json=manifest())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CatalogService(client).manifest()
        self.assertEqual(result.source, "official")
        self.assertEqual(visited, ["bmclapi2.bangbang93.com", "piston-meta.mojang.com"])
        self.assertEqual(result.diagnostics, ["bmclapi: HTTP 503"])

    async def test_malformed_json_triggers_fallback(self):
        def handle(request):
            if request.url.host == "bmclapi2.bangbang93.com":
                return httpx.Response(200, text="<html>not metadata</html>")
            return httpx.Response(200, json=manifest())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CatalogService(client).manifest()
        self.assertEqual(result.source, "official")
        self.assertEqual(len(result.diagnostics), 1)

    async def test_version_checks_hash_and_preserves_unknown_fields(self):
        def handle(request):
            if request.url.path.endswith("version_manifest_v2.json"):
                return httpx.Response(200, json=manifest())
            return httpx.Response(200, content=DETAIL)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CatalogService(client, SourcePolicy.OFFICIAL).version("1.20.1")
        self.assertEqual(result.metadata.javaVersion.majorVersion, 17)
        self.assertEqual(result.metadata.model_dump()["futureUpstreamField"], {"keep": True})

    async def test_corrupt_detail_falls_back_using_same_expected_hash(self):
        def handle(request):
            if request.url.path.endswith("version_manifest_v2.json"):
                return httpx.Response(200, json=manifest())
            content = b"corrupted" if request.url.host == "bmclapi2.bangbang93.com" else DETAIL
            return httpx.Response(200, content=content)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result = await CatalogService(client).version("1.20.1")
        self.assertEqual(result.source, "official")
        self.assertIn("SHA-1", result.diagnostics[0])

    async def test_hash_mismatch_never_returns_success(self):
        def handle(request):
            if request.url.path.endswith("version_manifest_v2.json"):
                return httpx.Response(200, json=manifest())
            return httpx.Response(200, content=DETAIL + b" ")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(MetadataError, "SHA-1"):
                await CatalogService(client, SourcePolicy.OFFICIAL).version("1.20.1")

    async def test_matching_hash_but_wrong_version_id_is_rejected(self):
        wrong = b'{"id":"wrong-version"}'

        def handle(request):
            if request.url.path.endswith("version_manifest_v2.json"):
                return httpx.Response(200, json=manifest(wrong))
            return httpx.Response(200, content=wrong)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(MetadataError, "ID"):
                await CatalogService(client, SourcePolicy.OFFICIAL).version("1.20.1")

    async def test_unknown_version_does_not_request_a_detail(self):
        visited = []

        def handle(request):
            visited.append(str(request.url))
            return httpx.Response(200, json=manifest())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(VersionNotFound):
                await CatalogService(client, SourcePolicy.OFFICIAL).version("nonexistent")
        self.assertEqual(visited, [MANIFEST_URL])

    async def test_cancellation_is_not_swallowed(self):
        def handle(request):
            raise asyncio.CancelledError

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(asyncio.CancelledError):
                await CatalogService(client).manifest()

    async def test_timeout_with_fixed_source_does_not_fallback(self):
        visited = []

        def handle(request):
            visited.append(request.url.host)
            raise httpx.ReadTimeout("test timeout", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaisesRegex(MetadataError, "test timeout"):
                await CatalogService(client, SourcePolicy.BMCLAPI).manifest()
        self.assertEqual(visited, ["bmclapi2.bangbang93.com"])

    async def test_oversize_metadata_is_rejected(self):
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 9))
        async with httpx.AsyncClient(transport=transport) as client:
            with patch("mcinst.catalog.MAX_METADATA_BYTES", 8):
                with self.assertRaisesRegex(MetadataError, "限制"):
                    await CatalogService(client, SourcePolicy.OFFICIAL).manifest()


class SourceTests(unittest.TestCase):
    def test_fixed_source_has_one_candidate(self):
        for policy in (SourcePolicy.OFFICIAL, SourcePolicy.BMCLAPI):
            with self.subTest(policy=policy):
                self.assertEqual(len(metadata_candidates(MANIFEST_URL, policy)), 1)

    def test_source_routing_rejects_unknown_or_unsafe_origins(self):
        for url in (
            "https://example.com/?url=https://piston-meta.mojang.com/",
            "https://piston-meta.mojang.com.evil.example/test.json",
            "https://user@piston-meta.mojang.com/test.json",
            "https://piston-meta.mojang.com:444/test.json",
            "http://piston-meta.mojang.com/test.json",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    metadata_candidates(url, SourcePolicy.AUTO)
