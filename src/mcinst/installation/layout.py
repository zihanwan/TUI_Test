import os
from dataclasses import dataclass
from pathlib import Path

from mcinst.installation.errors import InstallationError
from mcinst.installation.state import InstanceRecord, instance_lock, load_state, write_record


def game_root(directory: Path, record: InstanceRecord) -> Path:
    path = directory / record.minecraft_directory
    # A process interrupted between rename and record write can be completed by
    # migrate-layout. Do not silently create a second copy of the game tree.
    if record.minecraft_directory == "minecraft" and not path.exists() and not path.is_symlink():
        canonical = directory / ".minecraft"
        if canonical.exists() or canonical.is_symlink():
            raise InstallationError("游戏目录已更名但布局记录未更新，请运行 migrate-layout")
    return path


@dataclass(frozen=True)
class LayoutMigrationResult:
    directory: Path
    minecraft_directory: Path
    changed: bool


def migrate_layout(directory: Path) -> LayoutMigrationResult:
    """Rename the game root only; preserve pinned plan bytes and every game file."""
    try:
        directory = directory.resolve()
        if not directory.is_dir():
            raise InstallationError("实例目录不存在")
        with instance_lock(directory):
            state = load_state(directory)
            if state is None:
                raise InstallationError("该目录尚无安装记录")
            _, record = state
            target = directory / ".minecraft"
            source = directory / "minecraft"
            if target.is_symlink() or source.is_symlink():
                raise InstallationError("布局迁移不接受符号链接游戏目录")
            if record.minecraft_directory == ".minecraft":
                if not target.is_dir():
                    raise InstallationError("记录中的 .minecraft 游戏目录不存在")
                return LayoutMigrationResult(directory, target, False)
            source_exists, target_exists = source.exists(), target.exists()
            if source_exists and target_exists:
                raise InstallationError("minecraft 与 .minecraft 同时存在，无法自动合并")
            if source_exists and not source.is_dir():
                raise InstallationError("旧游戏路径不是目录")
            if not source_exists and not target.is_dir():
                raise InstallationError("没有可迁移的游戏目录")
            moved = False
            if source_exists:
                os.rename(source, target)
                moved = True
            updated = record.model_copy(update={"minecraft_directory": ".minecraft"})
            try:
                write_record(directory, updated)
            except BaseException as exc:
                if moved:
                    try:
                        os.rename(target, source)
                    except OSError as rollback_error:
                        exc.add_note(f"目录回滚失败，请重跑 migrate-layout：{rollback_error}")
                raise
            return LayoutMigrationResult(directory, target, True)
    except OSError as exc:
        raise InstallationError(f"布局迁移失败：{exc}") from exc
