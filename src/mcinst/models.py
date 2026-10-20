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
    diagnostics: list[str] = Field(default_factory=list)
