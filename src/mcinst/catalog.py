import hashlib
from collections.abc import Callable
from typing import TypeVar

import httpx
from pydantic import ValidationError

from mcinst.errors import MetadataError, VersionNotFound
from mcinst.models import ManifestResult, VersionManifest, VersionMetadata, VersionResult
from mcinst.sources import MANIFEST_URL, SourcePolicy, metadata_candidates

T = TypeVar("T")
MAX_METADATA_BYTES = 16 * 1024 * 1024


class CatalogService:
    """Read-only catalog. The caller owns and reuses the asynchronous HTTP client."""

    def __init__(self, client: httpx.AsyncClient, policy: SourcePolicy = SourcePolicy.AUTO):
        self.client = client
        self.policy = policy

    async def _fetch(
        self, url: str, parse: Callable[[bytes], T], expected_sha1: str | None = None
    ) -> tuple[T, str, str, list[str]]:
        failures: list[str] = []
        for candidate in metadata_candidates(url, self.policy):
            try:
                body = bytearray()
                async with self.client.stream("GET", candidate.url) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_METADATA_BYTES:
                            raise ValueError("元数据超过 16 MiB 限制")
                        body.extend(chunk)
                raw = bytes(body)
                if expected_sha1 and hashlib.sha1(raw).hexdigest() != expected_sha1:
                    raise ValueError("元数据 SHA-1 校验失败")
                return parse(raw), candidate.name, candidate.url, failures
            except (httpx.HTTPError, ValidationError, ValueError) as exc:
                # Do not catch cancellation or swallow programming errors.
                detail = (
                    f"HTTP {exc.response.status_code}"
                    if isinstance(exc, httpx.HTTPStatusError)
                    else str(exc) or type(exc).__name__
                )
                failures.append(f"{candidate.name}: {detail}")
        raise MetadataError("所有候选来源均失败：" + "；".join(failures))

    async def manifest(self) -> ManifestResult:
        manifest, source, url, diagnostics = await self._fetch(
            MANIFEST_URL, VersionManifest.model_validate_json
        )
        return ManifestResult(manifest=manifest, source=source, url=url, diagnostics=diagnostics)

    async def version(self, version_id: str) -> VersionResult:
        listing = await self.manifest()
        entry = next((v for v in listing.manifest.versions if v.id == version_id), None)
        if entry is None:
            raise VersionNotFound(f"版本不存在：{version_id}")

        def parse(raw: bytes) -> VersionMetadata:
            metadata = VersionMetadata.model_validate_json(raw)
            if metadata.id != entry.id:
                raise ValueError("版本详情的 ID 与版本清单不一致")
            return metadata

        metadata, source, url, diagnostics = await self._fetch(str(entry.url), parse, entry.sha1)
        return VersionResult(
            metadata=metadata,
            sha1=entry.sha1,
            source=source,
            url=url,
            diagnostics=listing.diagnostics + diagnostics,
        )
