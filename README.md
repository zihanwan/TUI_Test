# mcinst

Python Minecraft Java 版实例下载与安装工具。当前处于开发第一阶段：
已实现原版版本列表、版本详情、元数据校验和来源回退；尚未实现实例安装与 TUI。

## 开发运行

需要 Python 3.12+ 与 uv。

```sh
uv sync
uv run mcinst --help
uv run mcinst versions
uv run mcinst versions --all --search 1.21 --limit 10
uv run mcinst inspect 1.20.1
uv run mcinst inspect 1.20.1 --source official --json
uv run python -m mcinst --help
```

`--source auto` 为 BMCLAPI 优先、官方回退；`official` 和 `bmclapi` 为固定来源。
版本详情先按清单提供的 SHA-1 校验，再解析 JSON，并核对版本 ID。
版本列表沿用上游顺序，不按字符串或 SemVer 重排。未知详情字段会保留。
查询失败退出码为 1；参数错误为 2；用户中断为 130。

默认使用 HTTPX 的环境代理设置，网络超时为 20 秒、连接超时为 10 秒。
当前不提供持久缓存、同源重试或实例文件下载；不要把查询成功理解为安装成功。

## 验证

```sh
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

测试使用 HTTPX MockTransport，不依赖真实 Minecraft 下载源。
测试运行器采用 Python 标准库 unittest，不额外依赖 pytest。
`versions` / `inspect` 可用于手动验证真实源，但不作为每日离线测试的一部分。

本次开发验证：Python 3.14.6、HTTPX 0.28.1、Pydantic 2.13.4，16 项离线测试通过。
当前终端无法解析 PyPI，因此尚未生成 uv.lock、运行 Ruff 或验证可编辑包安装。
真实源请求及 Python 3.12 的最低版本验证也尚未完成。
本机现有依赖可用时，可直接从源目录验证，不必等待 uv 安装：

```sh
PYTHONPATH=src /Users/markwan/miniconda3/bin/python3.14 -m mcinst --help
PYTHONPATH=src /Users/markwan/miniconda3/bin/python3.14 -m unittest discover -s tests -v
```

## 结构

| 文件 | 职责 |
| --- | --- |
| `src/mcinst/cli.py` | 命令行参数与展示 |
| `src/mcinst/catalog.py` | 异步查询、来源回退、哈希验证 |
| `src/mcinst/models.py` | 元数据与查询结果模型 |
| `src/mcinst/sources.py` | 来源策略与受限地址映射 |
| `src/mcinst/errors.py` | 可由任意前端展示的错误 |
| `docs/roadmap.md` | 开发顺序、验收条件与后续设计 |

核心不依赖界面库。Textual 在接入 TUI 时添加；新职责出现后再拆分目录。

参考：[Mojang 版本清单](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json)、
[BMCLAPI 文档](https://bmclapidoc.bangbang93.com/)、
[HTTPX AsyncClient](https://www.python-httpx.org/async/)。
