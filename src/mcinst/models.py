from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

Sha1 = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class VersionEntry(BaseModel):
    id: str = Field(min_length=1)
    type: str
    url: HttpUrl
    sha1: Sha1
    releaseTime: datetime
    time: datetime


class LatestVersions(BaseModel):
    release: str
    snapshot: str


class VersionManifest(BaseModel):
    latest: LatestVersions
    versions: list[VersionEntry]


class JavaRequirement(BaseModel):
    component: str | None = None
    majorVersion: int = Field(gt=0)


class VersionMetadata(BaseModel):
    # Keep unknown upstream fields for the installation planner added in the next stage.
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1)
    type: str | None = None
    mainClass: str | None = None
    javaVersion: JavaRequirement | None = None
    downloads: dict[str, Any] = Field(default_factory=dict)
    libraries: list[dict[str, Any]] = Field(default_factory=list)
    assetIndex: dict[str, Any] | None = None


class ManifestResult(BaseModel):
    manifest: VersionManifest
    source: str
    url: str
    diagnostics: list[str] = Field(default_factory=list)


class VersionResult(BaseModel):
    metadata: VersionMetadata
    sha1: Sha1
    source: str
    url: str
    upstream_url: str | None = None
    size: int | None = None
    diagnostics: list[str] = Field(default_factory=list)


class DownloadInfo(BaseModel):
    url: HttpUrl
    sha1: Sha1 | None = None
    size: int | None = Field(default=None, ge=0, strict=True)
    path: str | None = None


class AssetIndexInfo(DownloadInfo):
    id: str = Field(min_length=1)
    sha1: Sha1
    size: int = Field(ge=0, strict=True)


class AssetObject(BaseModel):
    hash: Sha1
    size: int = Field(ge=0, strict=True)


class AssetIndex(BaseModel):
    objects: dict[str, AssetObject]
    virtual: bool = Field(default=False, strict=True)
    map_to_resources: bool = Field(default=False, strict=True)


class AssetIndexResult(BaseModel):
    index: AssetIndex
    source: str
    url: str
    diagnostics: list[str] = Field(default_factory=list)


class LoggingFile(DownloadInfo):
    id: str = Field(min_length=1)


class ClientLogging(BaseModel):
    file: LoggingFile
