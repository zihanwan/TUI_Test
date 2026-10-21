import hashlib
import os
import stat
import tempfile
import zipfile
from pathlib import Path

from mcinst.downloads.files import check_target
from mcinst.installation.errors import InstallationError
from mcinst.installation.models import InstallPlan, safe_relative_path
from mcinst.installation.state import ManagedFile

MAX_NATIVE_BYTES = 512 * 1024 * 1024
MAX_NATIVE_ENTRIES = 20000


class PathRegistry:
    def __init__(self):
        self.files: set[str] = set()
        self.directories: set[str] = set()

    def add(self, path: str) -> None:
        safe_relative_path(path)
        key = path.casefold()
        parents = ["/".join(key.split("/")[:i]) for i in range(1, len(key.split("/")))]
        if key in self.files or key in self.directories or any(p in self.files for p in parents):
            raise InstallationError(f"安装目标路径冲突：{path}")
        self.files.add(key)
        self.directories.update(parents)


def validate_plan(plan: InstallPlan) -> PathRegistry:
    paths = PathRegistry()
    artifacts = {a.path: a for a in plan.artifacts}
    version = plan.spec.minecraft_version
    for artifact in plan.artifacts:
        path = artifact.path
        paths.add(path)
        if artifact.kind == "client":
            valid = path == f"versions/{version}/{version}.jar"
        elif artifact.kind == "version_json":
            valid = path == f"versions/{version}/{version}.json"
        elif artifact.kind in ("library", "native"):
            valid = path.startswith("libraries/")
        elif artifact.kind == "asset":
            valid = bool(artifact.sha1) and path == (
                f"assets/objects/{artifact.sha1[:2]}/{artifact.sha1}"
            )
        elif artifact.kind == "asset_index":
            valid = path.startswith("assets/indexes/") and len(path.split("/")) == 3
        else:
            valid = path.startswith("assets/log_configs/") and len(path.split("/")) == 3
        if not valid:
            raise InstallationError(f"文件目标不符合其用途：{artifact.kind} / {path}")
    kinds = [a.kind for a in plan.artifacts]
    if (
        kinds.count("client") != 1
        or kinds.count("version_json") != 1
        or kinds.count("asset_index") != 1
    ):
        raise InstallationError("原版计划必须包含唯一的客户端、版本 JSON 和资源索引")
    for step in plan.native_extractions:
        source = artifacts.get(step.archive)
        if source is None or source.kind != "native":
            raise InstallationError("原生库解压来源不在下载计划中")
        if step.destination != f"versions/{version}/natives":
            raise InstallationError("原生库解压目录不符合实例版本")
    for step in plan.asset_copies:
        source = artifacts.get(step.source)
        if source is None or source.kind != "asset":
            raise InstallationError("资源映射来源不在下载计划中")
        parts = step.destination.split("/")
        valid = parts[0] == "resources" and len(parts) > 1
        valid |= parts[:2] == ["assets", "virtual"] and len(parts) > 3
        if not valid:
            raise InstallationError("资源映射不能写入用户存档或配置目录")
        paths.add(step.destination)
    return paths


def digest_file(path: Path, relative: str) -> ManagedFile:
    digest = hashlib.sha1(usedforsecurity=False)
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(64 * 1024):
            digest.update(block)
            size += len(block)
    return ManagedFile(path=relative, sha1=digest.hexdigest(), size=size)


def publish_copy(root: Path, source: Path, expected: ManagedFile) -> None:
    target = check_target(root, expected.path, create=True)
    if target.exists() and digest_file(target, expected.path) == expected:
        return
    descriptor, name = tempfile.mkstemp(prefix=".mcinst-copy-", suffix=".part", dir=target.parent)
    temporary = Path(name)
    try:
        digest = hashlib.sha1(usedforsecurity=False)
        size = 0
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as stream:
            while block := stream.read(64 * 1024):
                size += len(block)
                if size > expected.size:
                    raise InstallationError("后处理来源超过预期大小")
                output.write(block)
                digest.update(block)
            if size != expected.size or digest.hexdigest() != expected.sha1:
                raise InstallationError("后处理来源校验失败")
            output.flush()
            os.fsync(output.fileno())
        check_target(root, expected.path, create=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def postprocess(plan: InstallPlan, root: Path, work_parent: Path) -> list[ManagedFile]:
    """Prepare all native members before publishing; never use ZipFile.extractall."""
    paths = validate_plan(plan)
    native_files: dict[str, tuple[Path, ManagedFile]] = {}
    expanded_bytes = 0
    entries = 0
    try:
        with tempfile.TemporaryDirectory(prefix=".mcinst-native-", dir=work_parent) as work:
            staging = Path(work)
            for step in plan.native_extractions:
                archive = check_target(root, step.archive, create=False)
                with zipfile.ZipFile(archive) as jar:
                    entries += len(jar.infolist())
                    if entries > MAX_NATIVE_ENTRIES:
                        raise InstallationError("原生库归档条目过多")
                    for member in jar.infolist():
                        name = safe_relative_path(member.filename.rstrip("/"))
                        mode = member.external_attr >> 16
                        file_type = stat.S_IFMT(mode)
                        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                            raise InstallationError("原生库归档包含符号链接或特殊文件")
                        if member.is_dir() or name.startswith("META-INF/"):
                            continue
                        if any(name.startswith(prefix) for prefix in step.excludes):
                            continue
                        expanded_bytes += member.file_size
                        if expanded_bytes > MAX_NATIVE_BYTES:
                            raise InstallationError("原生库解压大小超过限制")
                        relative = f"{step.destination}/{name}"
                        destination = staging / str(entries) / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        size = 0
                        digest = hashlib.sha1(usedforsecurity=False)
                        with jar.open(member) as source, destination.open("wb") as output:
                            while block := source.read(64 * 1024):
                                size += len(block)
                                if size > member.file_size:
                                    raise InstallationError("归档条目实际大小不匹配")
                                output.write(block)
                                digest.update(block)
                        if size != member.file_size:
                            raise InstallationError("原生库归档条目不完整")
                        record = ManagedFile(path=relative, sha1=digest.hexdigest(), size=size)
                        previous = native_files.get(relative)
                        if previous:
                            if previous[1] != record:
                                raise InstallationError(f"多个原生库包含不同的同名文件：{relative}")
                            continue
                        paths.add(relative)
                        native_files[relative] = destination, record
            derived: list[ManagedFile] = []
            for source, record in native_files.values():
                publish_copy(root, source, record)
                derived.append(record)
            artifacts = {a.path: a for a in plan.artifacts}
            for step in plan.asset_copies:
                artifact = artifacts[step.source]
                if artifact.sha1 is None or artifact.size is None:
                    raise InstallationError("资源映射缺少内容校验信息")
                source = check_target(root, step.source, create=False)
                record = ManagedFile(
                    path=step.destination,
                    sha1=artifact.sha1,
                    size=artifact.size,
                )
                publish_copy(root, source, record)
                derived.append(record)
            return derived
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise InstallationError(f"实例后处理失败：{exc}") from exc
