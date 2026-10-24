import argparse
import asyncio
import importlib.util
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import httpx

from mcinst import __version__
from mcinst.catalog import CatalogService
from mcinst.downloads.settings import DEFAULT_CONCURRENCY, DEFAULT_PER_SOURCE
from mcinst.errors import McinstError
from mcinst.installation.installer import InstallProgress, VanillaInstaller
from mcinst.installation.layout import migrate_layout
from mcinst.installation.models import InstanceSpec, TargetPlatform
from mcinst.installation.planner import InstallPlanService
from mcinst.installation.state import finish_thread
from mcinst.installation.verify import verify_instance
from mcinst.sources import SourcePolicy


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return result


def feature_flag(value: str) -> tuple[str, bool]:
    name, separator, state = value.partition("=")
    if not name or not separator or state not in ("true", "false"):
        raise argparse.ArgumentTypeError("使用 NAME=true 或 NAME=false")
    return name, state == "true"


def concurrency_int(value: str) -> int:
    result = positive_int(value)
    if result > 64:
        raise argparse.ArgumentTypeError("并发数不能超过 64")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Minecraft 原版实例下载与安装工具")
    root.add_argument("--version", action="version", version=f"mcinst {__version__}")
    commands = root.add_subparsers(dest="command")
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--source", choices=list(SourcePolicy), default="auto")
    shared.add_argument("--json", action="store_true", help="输出机器可读的 JSON")
    versions = commands.add_parser("versions", parents=[shared], help="查询原版游戏版本")
    versions.add_argument("--all", action="store_true", help="包含快照和历史测试版本")
    versions.add_argument("--search", default="", help="按版本 ID 筛选")
    versions.add_argument("--limit", type=positive_int, default=20)
    inspect = commands.add_parser("inspect", parents=[shared], help="读取并校验指定版本的元数据")
    inspect.add_argument("version_id")
    target_options = argparse.ArgumentParser(add_help=False)
    target_options.add_argument("version_id")
    target_options.add_argument("--name", help="实例名称，默认使用版本 ID")
    target_options.add_argument("--os", choices=["windows", "linux", "osx"], help="默认当前系统")
    target_options.add_argument("--arch", choices=["x86", "x86_64", "amd64", "aarch64", "arm64"])
    target_options.add_argument("--os-version", help="用于匹配目标系统版本规则")
    target_options.add_argument(
        "--feature", type=feature_flag, action="append", default=[], metavar="NAME=BOOL"
    )
    plan = commands.add_parser(
        "plan",
        parents=[shared, target_options],
        help="生成原版安装计划，不下载游戏文件",
    )
    plan.add_argument("--files", action="store_true", help="文本输出中列出所有文件及后处理步骤")
    install = commands.add_parser(
        "install", parents=[shared, target_options], help="下载并组装原版实例"
    )
    install.add_argument(
        "--directory", type=Path, required=True, help="共享游戏根目录，例如 .minecraft"
    )
    install.add_argument(
        "--concurrency",
        type=concurrency_int,
        default=DEFAULT_CONCURRENCY,
        help=f"总下载并发，默认 {DEFAULT_CONCURRENCY}",
    )
    install.add_argument(
        "--per-source",
        type=concurrency_int,
        default=DEFAULT_PER_SOURCE,
        help=f"每来源并发，默认 {DEFAULT_PER_SOURCE}",
    )
    verify = commands.add_parser("verify", help="离线校验实例的受管理文件")
    verify.add_argument("directory", type=Path)
    verify.add_argument("version_id", nargs="?", help="版本 ID；仅有一个受管理版本时可省略")
    verify.add_argument("--json", action="store_true")
    migrate = commands.add_parser("migrate-layout", help="将旧独立实例合入共享游戏根目录")
    migrate.add_argument("directory", type=Path)
    migrate.add_argument(
        "--directory", dest="destination", type=Path, required=True, help="目标共享根目录"
    )
    migrate.add_argument("--json", action="store_true")
    tui = commands.add_parser("tui", help="打开终端界面")
    tui.add_argument("--directory", type=Path, default=Path("instances/.minecraft"))
    tui.add_argument("--source", choices=list(SourcePolicy), default="auto")
    return root


async def run(args: argparse.Namespace) -> int:
    if args.command == "migrate-layout":
        result = await finish_thread(migrate_layout, args.directory, args.destination)
        if args.json:
            print(
                json.dumps(
                    {
                        "directory": str(result.directory),
                        "minecraft_directory": str(result.minecraft_directory),
                        "changed": result.changed,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            label = "布局迁移完成" if result.changed else "已迁移至共享游戏根目录"
            print(f"{label}：{result.minecraft_directory}")
        return 0
    if args.command == "verify":
        result = await asyncio.to_thread(verify_instance, args.directory, args.version_id)
        if args.json:
            print(
                json.dumps(
                    {
                        **asdict(result),
                        "directory": str(result.directory),
                        "minecraft_directory": str(result.minecraft_directory),
                        "ok": result.ok,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"游戏根目录：{result.minecraft_directory}")
            print(f"状态：{result.status}；检查 {result.checked_files} 个文件")
            print(
                f"缺失 {len(result.missing)}，不匹配 {len(result.mismatched)}，"
                f"无预期哈希 {len(result.unverified)}"
            )
            for path in result.missing:
                print(f"[missing] {path}")
            for path in result.mismatched:
                print(f"[mismatched] {path}")
        return 0 if result.ok else 1
    spec = None
    if args.command in ("plan", "install"):
        spec = InstanceSpec(
            minecraft_version=args.version_id,
            name=args.name,
            target=TargetPlatform.detect(args.os, args.arch, args.os_version),
            features=dict(args.feature),
        )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=True,
        headers={"User-Agent": f"mcinst/{__version__}"},
    ) as client:
        catalog = CatalogService(client, SourcePolicy(args.source))
        if args.command == "versions":
            result = await catalog.manifest()
            # Preserve manifest order. Minecraft IDs are not universally SemVer.
            selected = [
                v
                for v in result.manifest.versions
                if (args.all or v.type == "release") and args.search in v.id
            ][: args.limit]
            if args.json:
                print(
                    json.dumps(
                        {
                            "latest": result.manifest.latest.model_dump(),
                            "versions": [v.model_dump(mode="json") for v in selected],
                            "source": result.source,
                            "diagnostics": result.diagnostics,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            else:
                print(f"来源：{result.source}  最新正式版：{result.manifest.latest.release}")
                for version in selected:
                    print(f"{version.id:<25} {version.type:<12} {version.releaseTime.date()}")
                if not selected:
                    print("没有匹配的版本。")
                for diagnostic in result.diagnostics:
                    print(f"来源回退：{diagnostic}", file=sys.stderr)
        elif args.command == "inspect":
            detail = await catalog.version(args.version_id)
            if args.json:
                print(detail.model_dump_json(indent=2))
            else:
                metadata = detail.metadata
                java = metadata.javaVersion
                print(f"版本：{metadata.id}\n来源：{detail.source}\nSHA-1：已校验")
                print(f"Java：{java.majorVersion if java else '元数据未声明，尚未应用兼容规则'}")
                print(f"依赖声明：{len(metadata.libraries)} 项（尚未按平台解析）")
                print("状态：仅查询元数据，尚未安装实例")
                for diagnostic in detail.diagnostics:
                    print(f"来源回退：{diagnostic}", file=sys.stderr)
        elif args.command == "plan":
            plan = await InstallPlanService(catalog).plan(spec)
            if args.json:
                print(plan.model_dump_json(indent=2))
            else:
                print(f"版本：{spec.minecraft_version}")
                print(f"目标：{spec.target.os} / {spec.target.arch}")
                print(f"依赖声明：{plan.declared_library_count} 项")
                print(f"下载文件：{len(plan.artifacts)} 个（相同目标已去重）")
                print(f"已知下载大小：{plan.known_download_bytes:,} 字节")
                print(f"大小未知：{plan.unknown_size_count} 个")
                print(f"原生库解压：{len(plan.native_extractions)} 项")
                print(f"资源映射：{len(plan.asset_copies)} 项")
                java = plan.java_requirement
                print(f"Java 要求：{java.majorVersion if java else '未知'}（尚未选择运行时）")
                print("状态：仅生成计划，尚未安装；路径相对于实例 Minecraft 根目录")
                if args.files:
                    for artifact in plan.artifacts:
                        size = artifact.size if artifact.size is not None else "未知大小"
                        print(f"[{artifact.kind}] {artifact.path}  {size}")
                    for step in plan.native_extractions:
                        print(f"[extract] {step.archive} -> {step.destination}")
                    for step in plan.asset_copies:
                        print(f"[copy] {step.source} -> {step.destination}")
                for diagnostic in plan.diagnostics:
                    print(f"提示：{diagnostic}", file=sys.stderr)
        elif args.command == "install":
            previous_stage = None
            last_report = 0.0

            def progress(event: InstallProgress) -> None:
                nonlocal previous_stage, last_report
                now = time.monotonic()
                if event.stage != previous_stage:
                    print(
                        {
                            "planning": "解析安装计划…",
                            "downloading": "下载与校验…",
                            "postprocessing": "解压原生库与映射资源…",
                        }[event.stage],
                        file=sys.stderr,
                    )
                    previous_stage = event.stage
                batch = event.downloads
                if batch and (
                    now - last_report >= 0.5 or batch.completed_files == batch.total_files
                ):
                    print(
                        f"文件 {batch.completed_files}/{batch.total_files}；"
                        f"下载 {batch.downloaded_files}，复用 {batch.reused_files}",
                        file=sys.stderr,
                    )
                    last_report = now

            result = await VanillaInstaller(
                catalog,
                concurrency=args.concurrency,
                per_source=args.per_source,
            ).install(spec, args.directory, progress)
            if args.json:
                payload = asdict(result)
                payload["instance_name"] = spec.instance_id
                payload["directory"] = str(result.directory)
                payload["minecraft_directory"] = str(result.minecraft_directory)
                print(json.dumps(payload, ensure_ascii=False, indent=2))
            else:
                print(
                    f"原版实例文件安装完成："
                    f"{result.minecraft_directory / 'versions' / spec.instance_id}"
                )
                print(
                    f"下载 {result.downloaded_files}，复用 {result.reused_files}，"
                    f"后处理文件 {result.derived_files}"
                )
                for diagnostic in result.diagnostics:
                    print(f"提示：{diagnostic}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command is None:
        root.print_help()
        return 0
    if args.command == "tui":
        if importlib.util.find_spec("textual") is None:
            print("缺少 Textual，请先运行 uv sync", file=sys.stderr)
            return 1
        from mcinst.ui.app import McinstApp

        McinstApp(args.directory, SourcePolicy(args.source)).run()
        return 0
    try:
        return asyncio.run(run(args))
    except (McinstError, httpx.HTTPError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
