import platform
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    computed_field,
    field_validator,
    model_validator,
)

from mcinst.models import JavaRequirement, Sha1
from mcinst.sources import SourceCandidate

OSName = Literal["windows", "linux", "osx"]
Architecture = Literal["x86", "x86_64", "aarch64"]


def safe_relative_path(value: str) -> str:
    """A portable relative path, never interpreted using the build host's OS."""
    if not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise ValueError(f"无效的相对路径：{value!r}")
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved |= {f"{prefix}{i}" for prefix in ("COM", "LPT") for i in range(1, 10)}
    for part in value.split("/"):
        if (
            part in ("", ".", "..")
            or part.endswith((".", " "))
            or any(c in part for c in '<>"|?*')
            or part.split(".")[0].upper() in reserved
        ):
            raise ValueError(f"无效的相对路径：{value!r}")
    return value


def safe_component(value: str) -> str:
    safe_relative_path(value)
    if "/" in value:
        raise ValueError(f"需要单个路径组件：{value!r}")
    return value


class TargetPlatform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    os: OSName
    arch: Architecture
    os_version: str | None = None

    @field_validator("arch", mode="before")
    @classmethod
    def normalize_arch(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("CPU 架构必须是字符串")
        aliases = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "i686": "x86"}
        return aliases.get(value.lower(), value.lower())

    @classmethod
    def detect(
        cls, os: str | None = None, arch: str | None = None, os_version: str | None = None
    ) -> "TargetPlatform":
        host_os = {"Windows": "windows", "Linux": "linux", "Darwin": "osx"}.get(platform.system())
        selected_os = os or host_os
        if os and os != host_os and arch is None:
            raise ValueError("跨系统生成计划必须显式指定 --arch")
        version = os_version
        if version is None and selected_os == host_os:
            if host_os == "osx":
                version = platform.mac_ver()[0] or None
            else:
                version = platform.version() if host_os == "windows" else platform.release()
        return cls(os=selected_os, arch=arch or platform.machine(), os_version=version)


class InstanceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minecraft_version: str
    name: str | None = None
    target: TargetPlatform
    features: dict[str, StrictBool] = Field(default_factory=dict)

    _validate_version = field_validator("minecraft_version")(safe_component)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is not None:
            safe_component(value)
            if value != value.strip() or len(value) > 64:
                raise ValueError("实例名称不能有首尾空白，且不能超过 64 个字符")
        return value

    @model_validator(mode="after")
    def normalize_name(self) -> "InstanceSpec":
        if self.name == self.minecraft_version:
            self.name = None
        return self

    @property
    def instance_id(self) -> str:
        return self.name or self.minecraft_version

    @property
    def version_json_path(self) -> str:
        name = self.instance_id
        return f"versions/{name}/{name}.json"

    @property
    def upstream_json_path(self) -> str:
        if self.name is not None:
            return f".mcinst/versions/{self.instance_id}/upstream.json"
        return self.version_json_path


class Artifact(BaseModel):
    kind: Literal["version_json", "client", "library", "native", "asset_index", "asset", "logging"]
    path: str
    upstream_url: str
    candidates: list[SourceCandidate]
    sha1: Sha1 | None
    size: int | None = Field(ge=0, strict=True)

    _validate_path = field_validator("path")(safe_relative_path)


class NativeExtraction(BaseModel):
    archive: str
    destination: str
    excludes: list[str] = Field(default_factory=list)

    _validate_paths = field_validator("archive", "destination")(safe_relative_path)


class AssetCopy(BaseModel):
    source: str
    destination: str

    _validate_paths = field_validator("source", "destination")(safe_relative_path)


class InstallPlan(BaseModel):
    schema_version: Literal[1] = 1
    spec: InstanceSpec
    artifacts: list[Artifact]
    native_extractions: list[NativeExtraction] = Field(default_factory=list)
    asset_copies: list[AssetCopy] = Field(default_factory=list)
    java_requirement: JavaRequirement | None = None
    diagnostics: list[str] = Field(default_factory=list)
    # Target files are relative to the Minecraft root, not the program's workspace.
    declared_library_count: int

    @computed_field
    @property
    def known_download_bytes(self) -> int:
        return sum(a.size for a in self.artifacts if a.size is not None)

    @computed_field
    @property
    def unknown_size_count(self) -> int:
        return sum(a.size is None for a in self.artifacts)
