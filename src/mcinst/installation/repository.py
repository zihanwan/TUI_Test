"""A shared Minecraft root, with one private installation record per version."""

from pathlib import Path
from unicodedata import normalize

from mcinst.installation.errors import InstallationError
from mcinst.installation.models import InstallPlan, safe_component
from mcinst.installation.postprocess import validate_plan
from mcinst.installation.state import load_state


def state_directory(root: Path, version: str, *, create: bool = False) -> Path:
    safe_component(version)
    current = root
    for component in (".mcinst", "versions", version):
        current = current / component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise InstallationError(f"安装状态路径必须是普通目录：{current}")
        if create:
            current.mkdir(exist_ok=True)
    return current


def managed_versions(root: Path) -> list[str]:
    parent = state_directory(root, "placeholder").parent
    if not parent.exists():
        return []
    versions = sorted(child.name for child in parent.iterdir())
    for version in versions:
        state_directory(root, version)
    return [version for version in versions if any((parent / version).iterdir())]


def check_repository(root: Path) -> None:
    if (root / "instance.json").exists():
        raise InstallationError("这是旧独立实例目录，请先用 migrate-layout 迁移至共享游戏根目录")
    journal = root / ".mcinst" / "migration.json"
    if journal.exists() or journal.is_symlink():
        raise InstallationError("存在未完成的布局迁移，请重跑原 migrate-layout 命令")


def check_instance_name(root: Path, name: str) -> None:
    """Reject spelling aliases that collide on Windows or normalization-aware disks."""
    safe_component(name)
    expected = normalize("NFC", name).casefold()
    for parent in (state_directory(root, "placeholder").parent, root / "versions"):
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise InstallationError(f"版本路径必须是普通目录：{parent}")
        if not parent.exists():
            continue
        for child in parent.iterdir():
            if child.name != name and normalize("NFC", child.name).casefold() == expected:
                raise InstallationError(f"实例名称已存在：{child.name}")


def select_state(root: Path, version: str | None) -> Path:
    check_repository(root)
    if version is None:
        versions = managed_versions(root)
        if len(versions) != 1:
            raise InstallationError(
                "请在 verify 的目录后指定版本 ID；受管理版本：" + ", ".join(versions)
            )
        version = versions[0]
    directory = state_directory(root, version)
    if not directory.is_dir():
        raise InstallationError(f"版本没有安装记录：{version}")
    return directory


def check_shared_files(root: Path, plan: InstallPlan) -> None:
    """A repair must not overwrite content pinned by a different managed version."""
    expected = {a.path: (a.sha1, a.size, a.upstream_url) for a in plan.artifacts}
    artifacts = {a.path: a for a in plan.artifacts}
    for step in plan.asset_copies:
        source = artifacts[step.source]
        expected[step.destination] = (source.sha1, source.size, source.upstream_url)
    for version in managed_versions(root):
        if version == plan.spec.instance_id:
            continue
        state = load_state(state_directory(root, version))
        if state is None:
            raise InstallationError(f"版本安装记录不完整：{version}")
        other, record = state
        validate_plan(other)
        if other.spec.instance_id != version or record.minecraft_directory != ".":
            raise InstallationError(f"版本安装记录与目录不一致：{version}")
        other_files = {item.path: item for item in [*other.artifacts, *record.derived_files]}
        for step in other.asset_copies:
            source = other_files[step.source]
            other_files[step.destination] = source.model_copy(update={"path": step.destination})
        for item in other_files.values():
            wanted = expected.get(item.path)
            if wanted is None:
                continue
            checksum, size, url = wanted
            compatible = checksum is not None and checksum == item.sha1
            if compatible and size is not None and item.size is not None:
                compatible = size == item.size
            if checksum is None and item.sha1 is None:
                compatible = url == getattr(item, "upstream_url", None) and size == item.size
            if not compatible:
                raise InstallationError(f"共享文件与版本 {version} 的固定内容冲突：{item.path}")
