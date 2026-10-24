import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from mcinst.downloads.files import check_target
from mcinst.installation.errors import InstallationError
from mcinst.installation.models import safe_component, safe_relative_path
from mcinst.installation.postprocess import digest_file, validate_plan
from mcinst.installation.repository import check_shared_files, state_directory
from mcinst.installation.state import (
    InstanceRecord,
    atomic_write,
    instance_lock,
    load_state,
    read_bytes,
    write_record,
)


def game_root(directory: Path, record: InstanceRecord) -> Path:
    if (directory / "migration-target.json").exists():
        raise InstallationError("该实例已迁移，请对新共享游戏根目录执行 verify")
    path = directory / record.minecraft_directory
    if record.minecraft_directory == "minecraft" and not path.exists():
        canonical = directory / ".minecraft"
        if canonical.exists() or canonical.is_symlink():
            raise InstallationError("游戏目录已更名但布局记录未更新，请运行 migrate-layout")
    return path


class MigrationJournal(BaseModel):
    source: str
    version: str
    files: list[str]
    directories: list[str] = Field(default_factory=list)

    _version = field_validator("version")(safe_component)

    @field_validator("files", "directories")
    @classmethod
    def validate_files(cls, values: list[str]) -> list[str]:
        for value in values:
            safe_relative_path(value)
            if value.split("/")[0] in {".mcinst", ".mcinst.lock"}:
                raise ValueError("旧游戏根目录含有保留的管理路径")
        return values


@dataclass(frozen=True)
class LayoutMigrationResult:
    directory: Path
    minecraft_directory: Path
    changed: bool


def migrate_layout(directory: Path, destination: Path) -> LayoutMigrationResult:
    """Move an old instance into a shared root; a journal permits offline recovery."""
    try:
        directory, destination = directory.resolve(), destination.resolve()
        if (
            directory == destination
            or directory in destination.parents
            or destination in directory.parents
        ):
            raise InstallationError("迁移源和目标必须是互不嵌套的目录")
        if not directory.is_dir():
            raise InstallationError("旧实例目录不存在")
        with instance_lock(directory), instance_lock(destination):
            if (destination / "instance.json").exists():
                raise InstallationError("迁移目标不能是旧独立实例目录")
            state = load_state(directory)
            if state is None:
                raise InstallationError("旧实例没有安装记录")
            plan, record = state
            validate_plan(plan)
            version = plan.spec.minecraft_version
            if record.minecraft_directory == ".":
                raise InstallationError("迁移源已经使用共享根目录布局")
            root = directory / record.minecraft_directory
            if (directory / "minecraft").exists() and (directory / ".minecraft").exists():
                raise InstallationError("minecraft 与 .minecraft 同时存在，无法确定迁移来源")
            if not root.exists() and record.minecraft_directory == "minecraft":
                root = directory / ".minecraft"
            if root.is_symlink():
                raise InstallationError("布局迁移不接受符号链接游戏目录")
            metadata = state_directory(destination, version, create=True)
            journal_path = destination / ".mcinst/migration.json"
            marker = directory / "migration-target.json"
            if not journal_path.exists() and marker.exists():
                target = MigrationJournal.model_validate_json(read_bytes(marker))
                if target.source != str(destination) or target.version != version:
                    raise InstallationError("旧实例已迁移至其他目标")
                if load_state(metadata) is None:
                    raise InstallationError("迁移目标记录丢失")
                return LayoutMigrationResult(directory, destination, False)
            if journal_path.exists():
                journal = MigrationJournal.model_validate_json(read_bytes(journal_path))
                if journal.source != str(directory) or journal.version != version:
                    raise InstallationError("目标有另一项未完成迁移，请先重跑对应迁移命令")
            else:
                if load_state(metadata) is not None:
                    raise InstallationError("目标已存在该版本的安装记录，拒绝覆盖")
                version_path = destination / "versions" / version
                if version_path.exists() or version_path.is_symlink():
                    raise InstallationError("目标已存在同名版本目录，拒绝覆盖")
                if not root.is_dir():
                    raise InstallationError("旧游戏根目录不存在")
                if root.stat().st_dev != destination.stat().st_dev:
                    raise InstallationError("当前迁移要求源和目标位于同一文件系统")
                files, directories = [], []
                for child in sorted(root.rglob("*")):
                    relative = safe_relative_path(child.relative_to(root).as_posix())
                    if child.is_symlink() or not (child.is_file() or child.is_dir()):
                        raise InstallationError(f"迁移不接受符号链接或特殊文件：{relative}")
                    if relative.split("/")[0] in {".mcinst", ".mcinst.lock"}:
                        raise InstallationError("旧游戏根目录含有保留的管理路径")
                    if child.is_file():
                        files.append(relative)
                    else:
                        directories.append(relative)
                journal = MigrationJournal(
                    source=str(directory), version=version, files=files, directories=directories
                )
                check_shared_files(destination, plan)
                check_migration_files(root, destination, journal)
                atomic_write(journal_path, journal.model_dump_json(indent=2).encode())
            check_migration_files(root, destination, journal)
            for relative in journal.directories:
                check_directory(destination, relative, create=True)
            for relative in journal.files:
                source = root / relative
                target = check_target(destination, relative, create=True)
                if source.exists() and not target.exists():
                    os.rename(source, target)
            # Preserve the exact pinned plan bytes, then commit its record last.
            atomic_write(
                metadata / "install-plan.json", read_bytes(directory / "install-plan.json")
            )
            write_record(metadata, record.model_copy(update={"minecraft_directory": "."}))
            atomic_write(
                marker,
                MigrationJournal(source=str(destination), version=version, files=[])
                .model_dump_json(indent=2)
                .encode(),
            )
            for relative in journal.files:
                source = root / relative
                if source.exists():
                    target = check_target(destination, relative, create=False)
                    if digest_file(source, relative) != digest_file(target, relative):
                        raise InstallationError(f"迁移期间文件发生变化：{relative}")
                    source.unlink()
            if root.exists():
                for child in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                    if child.is_dir() and not child.is_symlink():
                        child.rmdir()
                root.rmdir()
            journal_path.unlink()
            return LayoutMigrationResult(directory, destination, True)
    except (OSError, ValidationError) as exc:
        raise InstallationError(f"布局迁移失败，请重跑原迁移命令：{exc}") from exc


def check_migration_files(root: Path, destination: Path, journal: MigrationJournal) -> None:
    for relative in journal.directories:
        check_directory(destination, relative, create=False)
    for relative in journal.files:
        source = root / relative
        if source.exists() or source.is_symlink():
            source = check_target(root, relative, create=False)
        target = destination / relative
        current = destination
        for component in relative.split("/")[:-1]:
            current /= component
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise InstallationError(f"迁移目标父路径不是普通目录：{relative}")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise InstallationError(f"迁移目标不是普通文件：{relative}")
        if source.exists() and target.exists():
            if digest_file(source, relative) != digest_file(target, relative):
                raise InstallationError(f"迁移目标内容冲突：{relative}")
        elif not source.exists() and not target.exists():
            raise InstallationError(f"迁移文件在源和目标中均缺失：{relative}")


def check_directory(root: Path, relative: str, *, create: bool) -> None:
    current = root
    for component in relative.split("/"):
        current /= component
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise InstallationError(f"迁移目标不是普通目录：{relative}")
        if create:
            current.mkdir(exist_ok=True)
