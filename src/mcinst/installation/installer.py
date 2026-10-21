import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mcinst.catalog import CatalogService
from mcinst.downloads import ArtifactDownloader
from mcinst.downloads.files import resolve_root
from mcinst.downloads.limits import SourceLimiter
from mcinst.downloads.scheduler import BatchProgress, DownloadScheduler
from mcinst.installation.errors import InstallationError
from mcinst.installation.models import InstallPlan, InstanceSpec
from mcinst.installation.planner import InstallPlanService
from mcinst.installation.postprocess import postprocess, validate_plan
from mcinst.installation.state import (
    finish_thread,
    instance_lock,
    load_state,
    save_plan,
    write_record,
)
from mcinst.sources import download_candidates


@dataclass(frozen=True)
class InstallProgress:
    stage: str
    downloads: BatchProgress | None = None


@dataclass(frozen=True)
class InstallResult:
    directory: Path
    minecraft_directory: Path
    downloaded_files: int
    reused_files: int
    derived_files: int
    transferred_bytes: int
    diagnostics: tuple[str, ...]
    status: str = "installed"


class VanillaInstaller:
    def __init__(self, catalog: CatalogService, *, concurrency: int = 8, per_source: int = 4):
        self.catalog = catalog
        self.downloader = ArtifactDownloader(catalog.client, limiter=SourceLimiter(per_source))
        self.scheduler = DownloadScheduler(self.downloader, concurrency)

    async def install(
        self,
        spec: InstanceSpec,
        directory: Path,
        on_progress: Callable[[InstallProgress], None] | None = None,
    ) -> InstallResult:
        spec = InstanceSpec.model_validate(spec.model_dump())
        directory = await asyncio.to_thread(resolve_root, Path(directory))

        def report(stage: str, downloads: BatchProgress | None = None) -> None:
            if on_progress:
                on_progress(InstallProgress(stage, downloads))

        try:
            with instance_lock(directory):
                state = await finish_thread(load_state, directory)
                if state is None:
                    report("planning")
                    plan = await InstallPlanService(self.catalog).plan(spec)
                    validate_plan(plan)
                    record = await finish_thread(save_plan, directory, plan)
                else:
                    plan, record = state
                    if plan.spec != spec:
                        raise InstallationError(
                            "目标目录的固定版本或平台与本次请求不同，请选择新实例目录"
                        )
                    validate_plan(plan)
                # Change routing without changing pinned metadata, paths or hashes.
                plan = InstallPlan.model_validate(plan.model_dump())
                for artifact in plan.artifacts:
                    artifact.candidates = download_candidates(
                        artifact.upstream_url, self.catalog.policy
                    )
                record.status = "installing"
                record.error = None
                record.downloaded_files = 0
                record.reused_files = 0
                try:
                    await finish_thread(write_record, directory, record)
                    minecraft = directory / "minecraft"
                    await asyncio.to_thread(check_minecraft_root, minecraft)
                    report("downloading")

                    def batch_progress(event: BatchProgress) -> None:
                        record.downloaded_files = event.downloaded_files
                        record.reused_files = event.reused_files
                        report("downloading", event)

                    batch = await self.scheduler.run(
                        plan.artifacts,
                        minecraft,
                        batch_progress,
                    )
                    record.downloaded_files = sum(f.status == "downloaded" for f in batch.files)
                    record.reused_files = sum(f.status == "reused" for f in batch.files)
                    report("postprocessing")
                    record.derived_files = await finish_thread(
                        postprocess, plan, minecraft, directory
                    )
                    record.status = "installed"
                    await finish_thread(write_record, directory, record)
                    return InstallResult(
                        directory=directory,
                        minecraft_directory=minecraft,
                        downloaded_files=record.downloaded_files,
                        reused_files=record.reused_files,
                        derived_files=len(record.derived_files),
                        transferred_bytes=batch.transferred_bytes,
                        diagnostics=tuple(plan.diagnostics),
                    )
                except BaseException as exc:
                    record.status = (
                        "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                    )
                    record.error = str(exc) or type(exc).__name__
                    try:
                        await finish_thread(write_record, directory, record)
                    except Exception as state_error:
                        exc.add_note(f"无法保存失败状态：{state_error}")
                    raise
        except OSError as exc:
            raise InstallationError(f"实例文件操作失败：{exc}") from exc


def check_minecraft_root(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise InstallationError("实例 Minecraft 根目录必须是普通目录")
