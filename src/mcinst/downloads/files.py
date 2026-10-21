import hashlib
from pathlib import Path

from mcinst.downloads.errors import DownloadError
from mcinst.installation.models import Artifact, safe_relative_path


def resolve_root(root: Path) -> Path:
    try:
        return root.resolve()
    except OSError as exc:
        raise DownloadError(f"无法解析实例根目录：{root}") from exc


def check_target(root: Path, path: str, *, create: bool) -> Path:
    """Reject symlinks below the caller-selected, canonical Minecraft root."""
    safe_relative_path(path)
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise DownloadError(f"实例根目录不存在或不是目录：{root}")
    current = root
    for component in path.split("/")[:-1]:
        current = current / component
        if current.is_symlink():
            raise DownloadError(f"下载路径包含符号链接：{current}")
        if create:
            current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise DownloadError(f"下载父路径不是目录：{current}")
    target = current / path.split("/")[-1]
    if target.is_symlink():
        raise DownloadError(f"下载目标是符号链接：{target}")
    if target.exists() and not target.is_file():
        raise DownloadError(f"下载目标不是普通文件：{target}")
    return target


def existing_match(path: Path, artifact: Artifact) -> tuple[int, str] | None:
    # A matching size alone cannot establish content identity.
    if artifact.sha1 is None or not path.exists():
        return None
    if artifact.size is not None and path.stat().st_size != artifact.size:
        return None
    digest = hashlib.sha1(usedforsecurity=False)
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(256 * 1024):
            digest.update(block)
            size += len(block)
    checksum = digest.hexdigest()
    if checksum == artifact.sha1 and (artifact.size is None or artifact.size == size):
        return size, checksum
    return None
