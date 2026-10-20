import argparse
import asyncio
import json
import sys

import httpx

from mcinst import __version__
from mcinst.catalog import CatalogService
from mcinst.errors import McinstError
from mcinst.sources import SourcePolicy


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Minecraft 实例工具（当前阶段：原版元数据查询）")
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
    return root


async def run(args: argparse.Namespace) -> int:
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
                print(json.dumps({
                    "latest": result.manifest.latest.model_dump(),
                    "versions": [v.model_dump(mode="json") for v in selected],
                    "source": result.source,
                    "diagnostics": result.diagnostics,
                }, ensure_ascii=False, indent=2))
            else:
                print(f"来源：{result.source}  最新正式版：{result.manifest.latest.release}")
                for version in selected:
                    print(f"{version.id:<25} {version.type:<12} {version.releaseTime.date()}")
                if not selected:
                    print("没有匹配的版本。")
                for diagnostic in result.diagnostics:
                    print(f"来源回退：{diagnostic}", file=sys.stderr)
        else:
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
    return 0


def main(argv: list[str] | None = None) -> int:
    root = parser()
    args = root.parse_args(argv)
    if args.command is None:
        root.print_help()
        return 0
    try:
        return asyncio.run(run(args))
    except (McinstError, httpx.HTTPError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
