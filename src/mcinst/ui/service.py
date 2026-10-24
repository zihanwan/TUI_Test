from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from mcinst import __version__
from mcinst.catalog import CatalogService
from mcinst.downloads.files import resolve_root
from mcinst.installation.errors import InstallationError
from mcinst.installation.installer import InstallProgress, InstallResult, VanillaInstaller
from mcinst.installation.models import InstallPlan, InstanceSpec
from mcinst.installation.planner import InstallPlanService
from mcinst.installation.postprocess import validate_plan
from mcinst.installation.repository import (
    check_instance_name,
    check_repository,
    check_shared_files,
    managed_versions,
    state_directory,
)
from mcinst.installation.state import finish_thread, load_state
from mcinst.installation.verify import VerifyResult, verify_instance
from mcinst.models import ManifestResult, VersionResult
from mcinst.sources import SourcePolicy


@dataclass(frozen=True)
class LocalInstance:
    spec: InstanceSpec
    status: str

    @property
    def name(self) -> str:
        return self.spec.instance_id


def local_instances(root: Path) -> list[LocalInstance]:
    root = root.expanduser().resolve()
    check_repository(root)
    instances = []
    for name in managed_versions(root):
        state = load_state(state_directory(root, name))
        if state is None:
            continue
        plan, record = state
        validate_plan(plan)
        if plan.spec.instance_id != name or record.minecraft_directory != ".":
            raise InstallationError(f"实例记录与目录不一致：{name}")
        instances.append(LocalInstance(spec=plan.spec, status=record.status))
    return instances


def saved_plan(root: Path, spec: InstanceSpec, repair: bool) -> InstallPlan | None:
    check_repository(root)
    check_instance_name(root, spec.instance_id)
    metadata = state_directory(root, spec.instance_id)
    state = load_state(metadata) if metadata.is_dir() else None
    if state is not None:
        plan, record = state
        if not repair:
            raise InstallationError("实例名称已存在")
        if plan.spec != spec or record.minecraft_directory != ".":
            raise InstallationError("实例配置与固定计划不一致")
        validate_plan(plan)
        return plan
    if repair:
        raise InstallationError("实例安装记录不存在")
    version = root / "versions" / spec.instance_id
    if version.exists() or version.is_symlink():
        raise InstallationError("实例名称已存在")
    return None


class InterfaceService:
    """One client per operation; cancellation closes it after installation cleanup."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers={"User-Agent": f"mcinst/{__version__}"},
            transport=self.transport,
        )

    async def manifest(self, policy: SourcePolicy) -> ManifestResult:
        async with self.client() as client:
            return await CatalogService(client, policy).manifest()

    async def detail(self, version: str, policy: SourcePolicy) -> VersionResult:
        async with self.client() as client:
            return await CatalogService(client, policy).version(version)

    async def preview(
        self, root: Path, spec: InstanceSpec, policy: SourcePolicy, *, repair: bool = False
    ) -> InstallPlan:
        root = await finish_thread(resolve_root, root)
        plan = await finish_thread(saved_plan, root, spec, repair)
        if plan is None:
            async with self.client() as client:
                plan = await InstallPlanService(CatalogService(client, policy)).plan(spec)
        validate_plan(plan)
        await finish_thread(check_shared_files, root, plan)
        return plan

    async def install(
        self,
        root: Path,
        plan: InstallPlan,
        policy: SourcePolicy,
        concurrency: int,
        per_source: int,
        progress: Callable[[InstallProgress], None],
    ) -> InstallResult:
        async with self.client() as client:
            return await VanillaInstaller(
                CatalogService(client, policy), concurrency=concurrency, per_source=per_source
            ).install(plan.spec, root, progress, plan=plan)

    async def verify(self, root: Path, name: str) -> VerifyResult:
        return await finish_thread(verify_instance, root, name)
