from dataclasses import dataclass
from pathlib import Path

from mcinst.downloads.errors import DownloadError
from mcinst.downloads.files import check_target
from mcinst.installation.errors import InstallationError
from mcinst.installation.installer import check_minecraft_root
from mcinst.installation.layout import game_root
from mcinst.installation.postprocess import digest_file, validate_plan
from mcinst.installation.state import load_state


@dataclass(frozen=True)
class VerifyResult:
    directory: Path
    minecraft_directory: Path
    status: str
    checked_files: int
    missing: tuple[str, ...]
    mismatched: tuple[str, ...]
    unverified: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.status == "installed" and not self.missing and not self.mismatched


def verify_instance(directory: Path) -> VerifyResult:
    """Check managed files only. No network access or repairs."""
    try:
        directory = directory.resolve()
        state = load_state(directory)
        if state is None:
            raise InstallationError("该目录尚无安装记录")
        plan, record = state
        validate_plan(plan)
        root = game_root(directory, record)
        check_minecraft_root(root)
        missing, mismatched, unverified = [], [], []
        files = [*plan.artifacts, *record.derived_files]
        for expected in files:
            try:
                candidate = root / expected.path
                if not candidate.exists() and not candidate.is_symlink():
                    missing.append(expected.path)
                    continue
                target = check_target(root, expected.path, create=False)
                actual = digest_file(target, expected.path)
                if (expected.sha1 is not None and actual.sha1 != expected.sha1) or (
                    expected.size is not None and actual.size != expected.size
                ):
                    mismatched.append(expected.path)
                if expected.sha1 is None:
                    unverified.append(expected.path)
            except (DownloadError, OSError):
                mismatched.append(expected.path)
        return VerifyResult(
            directory=directory,
            minecraft_directory=root,
            status=record.status,
            checked_files=len(files),
            missing=tuple(missing),
            mismatched=tuple(mismatched),
            unverified=tuple(unverified),
        )
    except OSError as exc:
        raise InstallationError(f"无法校验实例：{exc}") from exc
