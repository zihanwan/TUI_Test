import re
from urllib.parse import quote

from pydantic import ValidationError

from mcinst.catalog import CatalogService
from mcinst.errors import PlanError
from mcinst.installation.models import (
    Artifact,
    AssetCopy,
    InstallPlan,
    InstanceSpec,
    NativeExtraction,
    safe_component,
    safe_relative_path,
)
from mcinst.installation.rules import rules_allow
from mcinst.models import AssetIndex, AssetIndexInfo, ClientLogging, DownloadInfo, VersionResult
from mcinst.sources import SourcePolicy, download_candidates


def maven_path(coordinate: str, classifier: str | None = None) -> str:
    """group:artifact:version[:classifier][@extension], independent of host OS."""
    if coordinate.count("@") > 1 or "$" in coordinate:
        raise PlanError(f"不支持的 Maven 坐标：{coordinate!r}")
    coordinate, separator, extension = coordinate.partition("@")
    parts = coordinate.split(":")
    if len(parts) not in (3, 4) or (separator and not extension):
        raise PlanError(f"不支持的 Maven 坐标：{coordinate!r}")
    group, artifact, version = parts[:3]
    selected_classifier = (
        classifier if classifier is not None else (parts[3] if len(parts) == 4 else None)
    )
    for component in [*group.split("."), artifact, version, extension or "jar"]:
        safe_component(component)
    suffix = ""
    if selected_classifier is not None:
        safe_component(selected_classifier)
        suffix = "-" + selected_classifier
    return safe_relative_path(
        f"{group.replace('.', '/')}/{artifact}/{version}/"
        f"{artifact}-{version}{suffix}.{extension or 'jar'}"
    )


def require_integrity(info: DownloadInfo, label: str) -> None:
    if info.sha1 is None or info.size is None:
        raise PlanError(f"{label} 缺少 SHA-1 或大小，无法生成完整计划")


class _PlanBuilder:
    def __init__(self, spec: InstanceSpec, policy: SourcePolicy):
        self.spec = spec
        self.policy = policy
        self.artifacts: dict[str, Artifact] = {}
        self.copies: list[AssetCopy] = []
        self.extractions: list[NativeExtraction] = []
        self.paths: dict[str, str] = {}
        self.parents: set[str] = set()
        self.diagnostics: list[str] = []

    def reserve(self, path: str) -> None:
        safe_relative_path(path)
        key = path.casefold()
        if key in self.paths or key in self.parents:
            raise PlanError(f"目标路径冲突：{path}")
        parts = key.split("/")
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts))]
        if any(prefix in self.paths for prefix in prefixes):
            raise PlanError(f"文件与目录路径冲突：{path}")
        self.paths[key] = path
        self.parents.update(prefixes)

    def add(self, kind: str, path: str, info: DownloadInfo) -> str:
        safe_relative_path(path)
        candidates = download_candidates(str(info.url), self.policy)
        # Preserve an HTTPS upstream identity even when mirror is the first candidate.
        upstream = download_candidates(str(info.url), SourcePolicy.OFFICIAL)[0].url
        artifact = Artifact(
            kind=kind,
            path=path,
            upstream_url=upstream,
            candidates=candidates,
            sha1=info.sha1,
            size=info.size,
        )
        existing = self.artifacts.get(path)
        if existing:
            if existing != artifact:
                raise PlanError(f"同一目标文件存在不同下载声明：{path}")
            return path
        self.reserve(path)
        self.artifacts[path] = artifact
        if info.sha1 is None or info.size is None:
            self.diagnostics.append(f"上游未提供完整校验信息：{path}")
        return path

    def copy(self, source: str, destination: str) -> None:
        self.reserve(destination)
        self.copies.append(AssetCopy(source=source, destination=destination))

    def libraries(self, libraries: list[dict], version_id: str) -> None:
        for library in libraries:
            if not rules_allow(library.get("rules"), self.spec):
                continue
            name = library.get("name")
            if not isinstance(name, str):
                raise PlanError("依赖库缺少 Maven name")
            fallback_path = maven_path(name)
            downloads = library.get("downloads")
            if "downloads" in library and downloads is None:
                raise PlanError(f"downloads 不能为 null：{name}")
            native_map = library.get("natives", {})
            if not isinstance(native_map, dict):
                raise PlanError(f"natives 必须是对象：{name}")
            classifier = None
            if native_map:
                classifier = native_map.get(self.spec.target.os)
                if not isinstance(classifier, str) or not classifier:
                    raise PlanError(f"原生库未提供目标平台：{name} / {self.spec.target.os}")
                bits = "32" if self.spec.target.arch == "x86" else "64"
                classifier = classifier.replace("${arch}", bits)
                if "$" in classifier:
                    raise PlanError(f"不支持的原生库占位符：{classifier}")
                if self.spec.target.arch == "aarch64" and not re.search(
                    "arm64|aarch64", classifier
                ):
                    raise PlanError(f"旧式原生库未声明 ARM64 支持：{name}")
            if "extract" in library and classifier is None:
                raise PlanError(f"解压声明缺少可识别的 natives classifier：{name}")
            if downloads is None:
                # Older descriptors lack hashes and sizes. Expose that uncertainty in the plan.
                base = library.get("url", "https://libraries.minecraft.net/")
                if not isinstance(base, str) or "?" in base or "#" in base:
                    raise PlanError(f"不支持的 Maven 仓库地址：{base!r}")
                artifact_info = DownloadInfo(url=base.rstrip("/") + "/" + quote(fallback_path))
                self.add("library", "libraries/" + fallback_path, artifact_info)
                native_info = (
                    DownloadInfo(url=base.rstrip("/") + "/" + quote(maven_path(name, classifier)))
                    if classifier
                    else None
                )
            else:
                if not isinstance(downloads, dict):
                    raise PlanError(f"downloads 必须是对象：{name}")
                artifact_data = downloads.get("artifact")
                if artifact_data is not None:
                    info = DownloadInfo.model_validate(artifact_data)
                    parts = name.split(":", 3)
                    native_coordinate = len(parts) == 4 and parts[3].startswith("natives-")
                    path = info.path if info.path is not None else fallback_path
                    self.add(
                        "native" if native_coordinate else "library", "libraries/" + path, info
                    )
                elif classifier is None:
                    raise PlanError(f"依赖库没有可用的下载声明：{name}")
                native_info = None
                if classifier:
                    classifiers = downloads.get("classifiers", {})
                    if not isinstance(classifiers, dict) or classifier not in classifiers:
                        raise PlanError(f"原生库缺少 classifier：{name} / {classifier}")
                    native_info = DownloadInfo.model_validate(classifiers[classifier])
            if native_info:
                native_path = native_info.path
                path = "libraries/" + (
                    native_path if native_path is not None else maven_path(name, classifier)
                )
                self.add("native", path, native_info)
                extract = library.get("extract", {})
                if not isinstance(extract, dict) or set(extract) - {"exclude"}:
                    raise PlanError(f"不支持的原生库解压规则：{name}")
                excludes = extract.get("exclude", [])
                if not isinstance(excludes, list) or any(not isinstance(e, str) for e in excludes):
                    raise PlanError(f"无效的解压排除规则：{name}")
                step = NativeExtraction(
                    archive=path,
                    destination=f"versions/{version_id}/natives",
                    excludes=excludes,
                )
                if step not in self.extractions:
                    self.extractions.append(step)

    def assets(self, index: AssetIndex, index_id: str) -> None:
        # Sorting makes JSON plans stable even if the index changes object key order.
        for name, obj in sorted(index.objects.items()):
            safe_relative_path(name)
            object_path = f"assets/objects/{obj.hash[:2]}/{obj.hash}"
            self.add(
                "asset",
                object_path,
                DownloadInfo(
                    url=f"https://resources.download.minecraft.net/{obj.hash[:2]}/{obj.hash}",
                    sha1=obj.hash,
                    size=obj.size,
                ),
            )
            if index.virtual:
                self.copy(object_path, f"assets/virtual/{index_id}/{name}")
            if index.map_to_resources:
                self.copy(object_path, f"resources/{name}")


def build_plan(
    spec: InstanceSpec,
    detail: VersionResult,
    index: AssetIndex,
    policy: SourcePolicy = SourcePolicy.AUTO,
    diagnostics: list[str] | None = None,
) -> InstallPlan:
    """Pure planning from already fetched metadata; no network or filesystem writes."""
    try:
        metadata = detail.metadata
        if metadata.id != spec.minecraft_version:
            raise PlanError("请求的版本与详情 ID 不一致")
        extra = metadata.model_extra or {}
        if "inheritsFrom" in extra or "jar" in extra:
            raise PlanError("当前规划器仅支持独立原版，不支持继承版本或 JAR 重定向")
        if metadata.assetIndex is None:
            raise PlanError("当前不支持缺少 assetIndex 的历史资源格式")
        index_info = AssetIndexInfo.model_validate(metadata.assetIndex)
        safe_component(index_info.id)
        if detail.upstream_url is None or detail.size is None:
            raise PlanError("版本详情缺少规范上游地址或原始大小，请通过 CatalogService 获取")
        builder = _PlanBuilder(spec, policy)
        builder.add(
            "version_json",
            spec.upstream_json_path,
            DownloadInfo(
                url=detail.upstream_url,
                sha1=detail.sha1,
                size=detail.size,
            ),
        )
        client = DownloadInfo.model_validate(metadata.downloads.get("client"))
        require_integrity(client, "客户端")
        name = spec.instance_id
        builder.add("client", f"versions/{name}/{name}.jar", client)
        builder.libraries(metadata.libraries, name)
        builder.add("asset_index", f"assets/indexes/{index_info.id}.json", index_info)
        builder.assets(index, index_info.id)
        logging = extra.get("logging", {})
        if not isinstance(logging, dict):
            raise PlanError("logging 必须是对象")
        if "client" in logging:
            info = ClientLogging.model_validate(logging["client"]).file
            safe_component(info.id)
            require_integrity(info, "日志配置")
            builder.add("logging", f"assets/log_configs/{info.id}", info)
        if metadata.javaVersion is None:
            builder.diagnostics.append("元数据未声明 Java 要求；本阶段不推断版本")
        builder.diagnostics.append("平台筛选遵循上游规则；未验证原生二进制或 Java 的运行兼容性")
        return InstallPlan(
            spec=spec,
            artifacts=list(builder.artifacts.values()),
            native_extractions=builder.extractions,
            asset_copies=builder.copies,
            java_requirement=metadata.javaVersion,
            declared_library_count=len(metadata.libraries),
            diagnostics=detail.diagnostics + (diagnostics or []) + builder.diagnostics,
        )
    except (ValidationError, ValueError) as exc:
        raise PlanError(f"无法生成安装计划：{exc}") from exc


class InstallPlanService:
    def __init__(self, catalog: CatalogService):
        self.catalog = catalog

    async def plan(self, spec: InstanceSpec) -> InstallPlan:
        detail = await self.catalog.version(spec.minecraft_version)
        try:
            extra = detail.metadata.model_extra or {}
            if "inheritsFrom" in extra or "jar" in extra:
                raise PlanError("当前规划器不支持继承版本或 JAR 重定向")
            if detail.metadata.assetIndex is None:
                raise PlanError("当前不支持缺少 assetIndex 的历史资源格式")
            info = AssetIndexInfo.model_validate(detail.metadata.assetIndex)
            safe_component(info.id)
        except (ValidationError, ValueError) as exc:
            raise PlanError(f"资源索引声明无效：{exc}") from exc
        result = await self.catalog.asset_index(info)
        return build_plan(spec, detail, result.index, self.catalog.policy, result.diagnostics)
