import asyncio
import hashlib
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from mcinst.installation.errors import InstallationError
from mcinst.installation.models import InstallPlan, safe_relative_path
from mcinst.models import Sha1

MAX_STATE_BYTES = 64 * 1024 * 1024


class ManagedFile(BaseModel):
    path: str
    sha1: Sha1
    size: int = Field(ge=0)

    _validate_path = field_validator("path")(safe_relative_path)


class InstanceRecord(BaseModel):
    schema_version: Literal[1] = 1
    status: Literal["installing", "installed", "failed", "cancelled"]
    plan_sha256: str
    # Records written before layout tracking used minecraft/. Keep their default.
    minecraft_directory: Literal["minecraft", ".minecraft", "."] = "minecraft"
    downloaded_files: int = 0
    reused_files: int = 0
    derived_files: list[ManagedFile] = Field(default_factory=list)
    error: str | None = None


def atomic_write(path: Path, data: bytes) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallationError(f"状态目标不是普通文件：{path}")
    descriptor, name = tempfile.mkstemp(prefix=".mcinst-state-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_record(directory: Path, record: InstanceRecord) -> None:
    atomic_write(directory / "instance.json", record.model_dump_json(indent=2).encode())


def read_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_STATE_BYTES:
        raise InstallationError(f"无效的实例状态文件：{path}")
    with path.open("rb") as stream:
        data = stream.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        raise InstallationError("实例状态文件过大")
    return data


def load_state(directory: Path) -> tuple[InstallPlan, InstanceRecord] | None:
    path = directory / "instance.json"
    if not path.exists() and not path.is_symlink():
        allowed = {".mcinst.lock"}
        if any(child.name not in allowed for child in directory.iterdir()):
            raise InstallationError("目标目录非空且不是受管理实例，请选择新的实例目录")
        return None
    try:
        record = InstanceRecord.model_validate_json(read_bytes(path))
        raw = read_bytes(directory / "install-plan.json")
        if hashlib.sha256(raw).hexdigest() != record.plan_sha256:
            raise InstallationError("安装计划校验失败，不能继续现有实例")
        plan = InstallPlan.model_validate_json(raw)
        return plan, record
    except (ValidationError, ValueError, OSError) as exc:
        raise InstallationError(f"无法读取实例状态：{exc}") from exc


def save_plan(directory: Path, plan: InstallPlan) -> InstanceRecord:
    raw = plan.model_dump_json(indent=2).encode()
    atomic_write(directory / "install-plan.json", raw)
    record = InstanceRecord(
        status="installing",
        plan_sha256=hashlib.sha256(raw).hexdigest(),
        minecraft_directory=".",
    )
    write_record(directory, record)
    return record


@contextmanager
def instance_lock(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".mcinst.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallationError("实例锁路径不是普通文件")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise InstallationError("该实例已有安装任务") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise InstallationError("该实例已有安装任务") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    # Keep the lock inode: deleting a lock file permits racing processes to lock different files.


async def finish_thread[T](function: Callable[..., T], *args) -> T:
    """On cancellation, wait for filesystem work before releasing the instance lock."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve an exception so the thread task never leaks an unobserved failure.
        if not task.cancelled():
            task.exception()
        raise
