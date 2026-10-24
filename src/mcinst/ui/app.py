import asyncio
import time
from pathlib import Path

import httpx
from pydantic import ValidationError
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Resize
from textual.widgets import (
    Button,
    Checkbox,
    Collapsible,
    ContentSwitcher,
    Footer,
    Input,
    Label,
    OptionList,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from mcinst.downloads.settings import DEFAULT_CONCURRENCY, DEFAULT_PER_SOURCE
from mcinst.errors import McinstError
from mcinst.installation.installer import InstallProgress
from mcinst.installation.models import InstallPlan, InstanceSpec, TargetPlatform
from mcinst.installation.state import finish_thread
from mcinst.models import VersionEntry
from mcinst.sources import SourcePolicy
from mcinst.ui.service import InterfaceService, LocalInstance, local_instances

EXPECTED_ERRORS = (McinstError, httpx.HTTPError, ValueError, OSError)
PLATFORMS = {
    "linux": ("linux", "x86_64"),
    "linux-arm64": ("linux", "aarch64"),
    "linux-x86": ("linux", "x86"),
    "windows": ("windows", "x86_64"),
    "windows-arm64": ("windows", "aarch64"),
    "windows-x86": ("windows", "x86"),
    "osx-arm64": ("osx", "aarch64"),
    "osx-x64": ("osx", "x86_64"),
}


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


class McinstApp(App):
    TITLE = "mcinst"
    CSS_PATH = "app.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("slash", "search", "搜索"),
        Binding("escape", "back", "返回"),
        Binding("ctrl+r", "refresh", "刷新"),
        Binding("ctrl+q", "safe_quit", "退出", priority=True),
        Binding("ctrl+c", "safe_quit", show=False, priority=True),
    ]

    def __init__(
        self,
        directory: Path = Path("instances/.minecraft"),
        policy: SourcePolicy = SourcePolicy.AUTO,
        *,
        service: InterfaceService | None = None,
    ):
        super().__init__()
        self.directory = directory.expanduser().resolve()
        self.policy = policy
        self.service = service or InterfaceService()
        self.entries: list[VersionEntry] = []
        self.visible_entries: list[VersionEntry] = []
        self.selected: VersionEntry | None = None
        self.instances: list[LocalInstance] = []
        self.repair: LocalInstance | None = None
        self.prepared: InstallPlan | None = None
        self.operation: Worker | None = None
        self.busy = False
        self.closing = False
        self.revision = 0
        self.last_progress = 0.0
        self.last_stage = ""
        self.ready = False
        self.form_guard = False
        self.drafts: dict[str, str] = {}
        self.target = TargetPlatform.detect()

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            yield Static("mcinst", id="brand")
            yield Button("下载", id="nav-download", classes="nav active")
            yield Button("实例", id="nav-local", classes="nav")
            yield Button("设置", id="nav-settings", classes="nav")
        yield Static(str(self.directory), id="root", markup=False)
        yield Static("", id="error", markup=False)
        with ContentSwitcher(initial="download", id="pages"):
            with Horizontal(id="download"):
                with Vertical(id="versions"):
                    yield Label("版本")
                    yield Input(placeholder="搜索版本", id="search")
                    yield Checkbox("快照", id="snapshots")
                    yield Static("", id="catalog-status", markup=False)
                    yield OptionList(id="version-list")
                with VerticalScroll(id="detail"):
                    yield Static("—", id="selected-version", markup=False)
                    yield Label("实例名称", classes="field-label")
                    yield Input(id="instance-name", max_length=64)
                    yield Static("", id="name-error", markup=False)
                    yield Static("", id="version-info", markup=False)
                    yield Static("", id="install-path", markup=False)
                    yield Label("下载源", classes="field-label")
                    yield Select(
                        [("自动", "auto"), ("官方", "official"), ("BMCLAPI", "bmclapi")],
                        value=self.policy.value,
                        allow_blank=False,
                        id="source",
                    )
                    yield Label("平台", classes="field-label")
                    yield Select(
                        [
                            ("本机", "native"),
                            ("Linux / x86_64", "linux"),
                            ("Linux / ARM64", "linux-arm64"),
                            ("Linux / x86", "linux-x86"),
                            ("Windows / x86_64", "windows"),
                            ("Windows / ARM64", "windows-arm64"),
                            ("Windows / x86", "windows-x86"),
                            ("macOS / ARM64", "osx-arm64"),
                            ("macOS / x86_64", "osx-x64"),
                        ],
                        value="native",
                        allow_blank=False,
                        id="platform",
                    )
                    with Collapsible(title="高级选项", collapsed=True, id="advanced"):
                        yield Label("系统版本")
                        yield Input(self.target.os_version or "", id="os-version")
                        yield Label("总并发")
                        yield Input(str(DEFAULT_CONCURRENCY), type="integer", id="concurrency")
                        yield Label("每来源并发")
                        yield Input(str(DEFAULT_PER_SOURCE), type="integer", id="per-source")
                    yield Button("预览计划", variant="primary", id="preview", disabled=True)
                    with Vertical(id="plan"):
                        yield Static("", id="plan-summary", markup=False)
                        with Horizontal(classes="actions"):
                            yield Button("安装", variant="primary", id="install")
                            yield Button("返回", id="back-config")
            with Vertical(id="local"):
                yield OptionList(id="instance-list")
                yield Static("", id="verify-result", markup=False)
                with Horizontal(classes="actions"):
                    yield Button("校验", id="verify", disabled=True)
                    yield Button("修复", id="repair", disabled=True)
                    yield Button("刷新", id="refresh-local")
            with VerticalScroll(id="settings"):
                yield Label("游戏目录")
                yield Input(str(self.directory), id="root-input")
                yield Button("应用", id="save-settings")
        with Vertical(id="task"):
            yield Static("", id="task-title", markup=False)
            yield ProgressBar(total=100, show_eta=False, id="progress")
            yield Static("", id="task-counts", markup=False)
            with Horizontal(classes="actions"):
                yield Button("日志", id="toggle-log")
                yield Button("取消", id="cancel")
            yield RichLog(id="logs", markup=False, highlight=False, max_lines=300)
        yield Footer()

    def on_mount(self) -> None:
        for selector in ("#error", "#name-error", "#plan", "#task", "#logs"):
            self.query_one(selector).display = False
        self.set_class(self.size.width < 90, "narrow")
        self.ready = True
        self.load_manifest()
        self.load_instances()
        self.query_one("#version-list").focus()

    def on_resize(self, event: Resize) -> None:
        self.set_class(event.size.width < 90, "narrow")

    def error(self, message: str = "") -> None:
        widget = self.query_one("#error", Static)
        widget.update(message)
        widget.display = bool(message)
        if message:
            self.query_one("#logs", RichLog).write(Text(message))

    def invalidate(self) -> None:
        self.revision += 1
        self.prepared = None
        self.workers.cancel_group(self, "preview")
        self.query_one("#plan").display = False
        self.query_one("#preview").display = True
        self.validate_form()

    def show_page(self, page: str) -> None:
        self.query_one("#pages", ContentSwitcher).current = page
        for button in self.query(".nav"):
            button.set_class(button.id == f"nav-{page}", "active")

    def form_spec(self) -> InstanceSpec:
        if self.repair:
            return self.repair.spec
        if self.selected is None:
            raise ValueError("请选择版本")
        name = self.query_one("#instance-name", Input).value
        # An empty value must never silently become the upstream version ID.
        if not name:
            raise ValueError("实例名称不能为空")
        return InstanceSpec(
            minecraft_version=self.selected.id,
            name=name,
            target=self.repair.spec.target if self.repair else self.target,
            features=self.repair.spec.features if self.repair else {},
        )

    def validate_form(self) -> bool:
        try:
            spec = self.form_spec()
            if not self.repair and any(
                instance.name.casefold() == spec.instance_id.casefold()
                for instance in self.instances
            ):
                raise ValueError("实例名称已存在")
            problem = ""
        except ValueError as exc:
            problem = (
                exc.errors()[0]["msg"].removeprefix("Value error, ")
                if isinstance(exc, ValidationError)
                else str(exc)
            )
        widget = self.query_one("#name-error", Static)
        widget.update(problem)
        widget.display = bool(problem) and (self.selected is not None or self.repair is not None)
        self.query_one("#preview", Button).disabled = bool(problem) or self.busy
        name = self.query_one("#instance-name", Input).value
        self.query_one("#install-path", Static).update(f"versions/{name}/" if not problem else "")
        return not problem

    def select_version(self, entry: VersionEntry) -> None:
        if self.form_guard or self.busy:
            return
        self.form_guard = True
        self.selected = entry
        self.repair = None
        self.invalidate()
        self.error()
        self.query_one("#instance-name", Input).disabled = False
        name = self.drafts.get(entry.id, entry.id)
        occupied = {instance.name.casefold() for instance in self.instances}
        base, suffix = name, 2
        while name.casefold() in occupied:
            name = f"{base}-{suffix}"
            suffix += 1
        with self.prevent(Input.Changed):
            self.query_one("#instance-name", Input).value = name
        self.query_one("#selected-version", Static).update(entry.id)
        self.query_one("#version-info", Static).update("原版 · Java —")
        self.query_one("#install", Button).label = "安装"
        self.query_one("#platform", Select).disabled = False
        self.query_one("#os-version", Input).disabled = False
        self.form_guard = False
        self.validate_form()
        self.load_detail(entry.id, self.policy)

    def filter_versions(self) -> None:
        query = self.query_one("#search", Input).value.casefold()
        snapshots = self.query_one("#snapshots", Checkbox).value
        self.visible_entries = [
            entry
            for entry in self.entries
            if (entry.type == "release" or (snapshots and entry.type == "snapshot"))
            and query in entry.id.casefold()
        ]
        options = self.query_one("#version-list", OptionList)
        options.clear_options()
        options.add_options(
            Option(
                Text(f"{entry.id}  {'正式版' if entry.type == 'release' else entry.type}"),
                id=str(i),
            )
            for i, entry in enumerate(self.visible_entries)
        )
        self.query_one("#catalog-status", Static).update("" if self.visible_entries else "无结果")
        if self.visible_entries:
            options.highlighted = 0
        else:
            self.selected = None
            self.invalidate()
            self.validate_form()

    @work(exclusive=True, group="catalog", exit_on_error=False)
    async def load_manifest(self) -> None:
        policy = self.policy
        self.query_one("#catalog-status", Static).update("加载中…")
        self.entries = []
        self.selected = None
        self.invalidate()
        self.query_one("#version-list", OptionList).clear_options()
        self.validate_form()
        try:
            result = await self.service.manifest(policy)
            if policy != self.policy:
                return
            self.entries = result.manifest.versions
            self.filter_versions()
            for message in result.diagnostics:
                self.query_one("#logs", RichLog).write(Text(message))
        except EXPECTED_ERRORS as exc:
            self.query_one("#catalog-status", Static).update("加载失败")
            self.error(str(exc))

    @work(exclusive=True, group="details", exit_on_error=False)
    async def load_detail(self, version: str, policy: SourcePolicy) -> None:
        try:
            detail = await self.service.detail(version, policy)
            if self.selected is None or self.selected.id != version or self.policy != policy:
                return
            java = detail.metadata.javaVersion
            self.query_one("#version-info", Static).update(
                f"原版 · Java {java.majorVersion if java else '—'}"
            )
        except EXPECTED_ERRORS as exc:
            if self.selected and self.selected.id == version and self.policy == policy:
                self.error(str(exc))

    @work(exclusive=True, group="local", exit_on_error=False)
    async def load_instances(self) -> None:
        directory = self.directory
        try:
            instances = await finish_thread(local_instances, directory)
            if directory != self.directory:
                return
            self.instances = instances
            options = self.query_one("#instance-list", OptionList)
            options.clear_options()
            options.add_options(
                Option(
                    Text(
                        f"{item.name}  {item.status}\n"
                        f"{item.spec.minecraft_version} · "
                        f"{item.spec.target.os} / {item.spec.target.arch}"
                    ),
                    id=str(i),
                )
                for i, item in enumerate(instances)
            )
            if instances:
                options.highlighted = 0
            self.update_local_actions()
            self.validate_form()
        except EXPECTED_ERRORS as exc:
            self.error(str(exc))

    def selected_instance(self) -> LocalInstance | None:
        index = self.query_one("#instance-list", OptionList).highlighted
        return self.instances[index] if index is not None and index < len(self.instances) else None

    def update_local_actions(self) -> None:
        disabled = self.busy or self.selected_instance() is None
        self.query_one("#verify", Button).disabled = disabled
        self.query_one("#repair", Button).disabled = disabled

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        for selector in (
            "#versions",
            "#instance-name",
            "#source",
            "#platform",
            "#advanced",
            "#save-settings",
            "#root-input",
            "#refresh-local",
        ):
            self.query_one(selector).disabled = busy
        if not busy and self.repair:
            for selector in ("#instance-name", "#platform", "#os-version"):
                self.query_one(selector).disabled = True
        self.query_one("#install", Button).disabled = busy
        self.query_one("#cancel", Button).disabled = not busy
        self.validate_form()
        self.update_local_actions()

    @work(exclusive=True, group="preview", exit_on_error=False)
    async def preview_plan(self) -> None:
        revision = self.revision
        try:
            spec = self.form_spec()
            self.query_one("#preview", Button).disabled = True
            self.error()
            plan = await self.service.preview(
                self.directory, spec, self.policy, repair=self.repair is not None
            )
            if revision != self.revision:
                return
            self.prepared = plan
            size = format_bytes(plan.known_download_bytes)
            if plan.unknown_size_count:
                size += f" + {plan.unknown_size_count} 个大小未知文件"
            self.query_one("#plan-summary", Static).update(
                f"{spec.instance_id}\n{spec.minecraft_version} · "
                f"{len(plan.artifacts):,} 文件 · {size}"
            )
            self.query_one("#plan").display = True
            self.query_one("#preview").display = False
            self.query_one("#install").focus()
            for message in plan.diagnostics:
                self.query_one("#logs", RichLog).write(Text(message))
        except EXPECTED_ERRORS as exc:
            if revision == self.revision:
                self.error(str(exc))
        finally:
            if revision == self.revision:
                self.validate_form()

    def progress(self, event: InstallProgress) -> None:
        batch = event.downloads
        now = time.monotonic()
        if event.stage == self.last_stage and now - self.last_progress < 0.1:
            if batch is None or batch.completed_files != batch.total_files:
                return
        self.last_stage, self.last_progress = event.stage, now
        stage = {"planning": "解析", "downloading": "下载", "postprocessing": "后处理"}[event.stage]
        name = self.prepared.spec.instance_id if self.prepared else ""
        self.query_one("#task-title", Static).update(f"{name} · {stage}")
        if batch:
            self.query_one("#progress", ProgressBar).update(
                total=batch.total_files, progress=batch.completed_files
            )
            self.query_one("#task-counts", Static).update(
                f"文件 {batch.completed_files:,}/{batch.total_files:,} · "
                f"下载 {batch.downloaded_files:,} · 复用 {batch.reused_files:,} · "
                f"传输 {format_bytes(batch.transferred_bytes)}"
            )

    @work(group="operation", exit_on_error=False)
    async def install_plan(self, plan: InstallPlan, concurrency: int, per_source: int) -> None:
        try:
            result = await self.service.install(
                self.directory, plan, self.policy, concurrency, per_source, self.progress
            )
            self.query_one("#task-title", Static).update(f"{plan.spec.instance_id} · 完成")
            self.query_one("#logs", RichLog).write(
                Text(
                    f"{plan.spec.instance_id}  下载 {result.downloaded_files}  "
                    f"复用 {result.reused_files}"
                )
            )
            self.prepared = None
        except asyncio.CancelledError:
            self.query_one("#task-title", Static).update(f"{plan.spec.instance_id} · 已取消")
            raise
        except EXPECTED_ERRORS as exc:
            self.query_one("#task-title", Static).update(f"{plan.spec.instance_id} · 失败")
            self.error(str(exc))
        finally:
            self.set_busy(False)
            self.invalidate()
            self.load_instances()
            if self.closing:
                self.exit()

    @work(group="operation", exit_on_error=False)
    async def verify_selected(self, item: LocalInstance) -> None:
        try:
            result = await self.service.verify(self.directory, item.name)
            self.query_one("#verify-result", Static).update(
                f"{item.name} · {result.status} · {result.checked_files:,} 文件 · "
                f"缺失 {len(result.missing)} · 不匹配 {len(result.mismatched)} · "
                f"无哈希 {len(result.unverified)}"
            )
            for path in result.missing:
                self.query_one("#logs", RichLog).write(Text(f"missing  {path}"))
            for path in result.mismatched:
                self.query_one("#logs", RichLog).write(Text(f"mismatched  {path}"))
        except EXPECTED_ERRORS as exc:
            self.error(str(exc))
        finally:
            self.set_busy(False)
            if self.closing:
                self.exit()

    @on(Input.Changed)
    def input_changed(self, event: Input.Changed) -> None:
        if not self.ready or self.form_guard:
            return
        if event.input.id == "search":
            self.filter_versions()
        elif event.input.id in ("instance-name", "os-version", "concurrency", "per-source"):
            self.invalidate()
            if event.input.id == "instance-name" and self.selected:
                self.drafts[self.selected.id] = event.value
            if event.input.id == "os-version" and not self.repair:
                self.target = self.target.model_copy(update={"os_version": event.value or None})
            self.validate_form()

    @on(Checkbox.Changed, "#snapshots")
    def snapshots_changed(self) -> None:
        if self.ready:
            self.filter_versions()

    @on(Select.Changed)
    def select_changed(self, event: Select.Changed) -> None:
        if not self.ready or self.form_guard:
            return
        self.invalidate()
        if event.select.id == "source":
            self.policy = SourcePolicy(event.value)
            if not self.repair:
                self.load_manifest()
        elif event.select.id == "platform" and not self.repair:
            self.target = (
                TargetPlatform.detect()
                if event.value == "native"
                else TargetPlatform.detect(*PLATFORMS[str(event.value)])
            )
            self.query_one("#os-version", Input).value = self.target.os_version or ""

    @on(OptionList.OptionHighlighted, "#version-list")
    def version_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option_index < len(self.visible_entries):
            self.select_version(self.visible_entries[event.option_index])

    @on(OptionList.OptionSelected, "#version-list")
    def version_selected(self) -> None:
        if self.validate_form() and not self.busy:
            self.preview_plan()

    @on(OptionList.OptionHighlighted, "#instance-list")
    def instance_highlighted(self) -> None:
        self.update_local_actions()

    @on(Button.Pressed)
    def button_pressed(self, event: Button.Pressed) -> None:
        identity = event.button.id
        if identity and identity.startswith("nav-"):
            self.show_page(identity.removeprefix("nav-"))
        elif identity == "preview" and not self.busy:
            self.preview_plan()
        elif identity == "back-config":
            self.invalidate()
        elif identity == "install" and not self.busy and self.prepared:
            try:
                limits = [
                    int(self.query_one(f"#{field}", Input).value)
                    for field in ("concurrency", "per-source")
                ]
                if not all(1 <= value <= 64 for value in limits):
                    raise ValueError("并发数必须在 1～64 之间")
            except ValueError:
                self.error("并发数必须在 1～64 之间")
                return
            self.error()
            self.set_busy(True)
            self.last_stage = ""
            self.query_one("#task").display = True
            self.query_one("#progress", ProgressBar).update(total=100, progress=0)
            self.query_one("#task-title", Static).update(f"{self.prepared.spec.instance_id} · 下载")
            self.query_one("#task-counts", Static).update("")
            self.operation = self.install_plan(self.prepared, *limits)
        elif identity == "cancel":
            self.cancel_operation()
        elif identity == "toggle-log":
            logs = self.query_one("#logs")
            logs.display = not logs.display
        elif identity == "refresh-local" and not self.busy:
            self.load_instances()
        elif identity == "verify" and not self.busy and (item := self.selected_instance()):
            self.set_busy(True)
            self.operation = self.verify_selected(item)
        elif identity == "repair" and not self.busy and (item := self.selected_instance()):
            self.begin_repair(item)
        elif identity == "save-settings" and not self.busy:
            value = self.query_one("#root-input", Input).value.strip()
            if not value:
                self.error("游戏目录不能为空")
                return
            self.directory = Path(value).expanduser().resolve()
            self.query_one("#root", Static).update(str(self.directory))
            self.repair = None
            self.instances = []
            for selector in ("#instance-name", "#platform", "#os-version"):
                self.query_one(selector).disabled = False
            self.invalidate()
            self.load_instances()
            self.show_page("download")

    def begin_repair(self, item: LocalInstance) -> None:
        self.error()
        self.form_guard = True
        self.workers.cancel_group(self, "details")
        self.workers.cancel_group(self, "catalog")
        self.repair = item
        self.selected = next(
            (entry for entry in self.entries if entry.id == item.spec.minecraft_version), None
        )
        with self.prevent(Input.Changed, Select.Changed):
            self.query_one("#instance-name", Input).value = item.name
            platform = next(
                key
                for key, value in PLATFORMS.items()
                if value == (item.spec.target.os, item.spec.target.arch)
            )
            self.query_one("#platform", Select).value = platform
            self.query_one("#os-version", Input).value = item.spec.target.os_version or ""
        self.query_one("#selected-version", Static).update(item.spec.minecraft_version)
        self.query_one("#version-info", Static).update(
            f"原版 · {item.spec.target.os} / {item.spec.target.arch}"
        )
        self.query_one("#install", Button).label = "修复"
        for selector in ("#instance-name", "#platform", "#os-version"):
            self.query_one(selector).disabled = True
        self.form_guard = False
        self.invalidate()
        self.show_page("download")
        self.validate_form()
        self.preview_plan()

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            failure = event.worker.error
            self.error(f"{type(failure).__name__}: {failure}")
        if event.worker is self.operation and event.state in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            # Also handles cancellation before the coroutine's first instruction.
            if self.busy:
                self.set_busy(False)
            if self.closing:
                self.exit()

    def cancel_operation(self) -> None:
        if self.busy and self.operation:
            self.operation.cancel()
            self.query_one("#cancel", Button).disabled = True

    def action_search(self) -> None:
        self.show_page("download")
        self.query_one("#search").focus()

    def action_back(self) -> None:
        if not self.busy:
            self.invalidate()
        self.show_page("download")

    def action_refresh(self) -> None:
        if not self.busy:
            self.error()
            if self.query_one("#pages", ContentSwitcher).current == "local":
                self.load_instances()
            else:
                self.load_manifest()

    def action_safe_quit(self) -> None:
        if self.busy:
            self.closing = True
            self.cancel_operation()
        else:
            self.exit()
