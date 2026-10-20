# mcinst

Python Minecraft Java 版实例下载与安装工具。当前已实现原版版本查询与安装计划：
可预览目标平台需要的文件和后处理步骤；尚未实现实例安装与 TUI。

## 开发运行

需要 Python 3.12+ 与 uv。

```sh
uv sync
uv run mcinst --help
uv run mcinst versions
uv run mcinst versions --all --search 1.21 --limit 10
uv run mcinst inspect 1.20.1
uv run mcinst inspect 1.20.1 --source official --json
uv run mcinst plan 1.20.1 --os linux --arch x86_64 --files
uv run mcinst plan 1.20.1 --os windows --arch x86_64 --os-version 10.0 --json
uv run python -m mcinst --help
```

`--source auto` 为 BMCLAPI 优先、官方回退；`official` 和 `bmclapi` 为固定来源。
版本详情先按清单提供的 SHA-1 校验，再解析 JSON，并核对版本 ID。
版本列表沿用上游顺序，不按字符串或 SemVer 重排。未知详情字段会保留。
查询失败退出码为 1；参数错误为 2；用户中断为 130。

默认使用 HTTPX 的环境代理设置，网络超时为 20 秒、连接超时为 10 秒。
当前不提供持久缓存、同源重试或实例文件下载；不要把查询成功理解为安装成功。

`plan` 仅读取版本清单、版本详情和资源索引，不写实例目录。
文件路径相对于实例的 Minecraft 根目录，JSON 包含规范上游地址、候选来源、
哈希、大小、原生库解压和旧式资源映射步骤。已知总大小是从零安装的文件总量，
不扣除已读取的元数据或本地缓存。
未指定目标时使用本机系统与架构；跨系统生成计划必须显式指定 `--arch`。
`--feature NAME=true` 可设置影响库选择的功能条件，未提供的功能默认为 false。
旧 Maven 声明缺少哈希或大小时，会以 null 和诊断信息保留；未提供 Java 要求时仍为未知。
支持边界及 Review 入口见 [InstallPlan 说明](docs/install-plan.md)。

## 验证

```sh
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

测试使用 HTTPX MockTransport，不依赖真实 Minecraft 下载源。
测试运行器采用 Python 标准库 unittest，不额外依赖 pytest。
`versions` / `inspect` / `plan` 可用于手动验证真实源，不作为每日离线测试的一部分。

本次开发验证：43 项离线测试、Ruff 检查和格式检查通过，项目虚拟环境入口可用。
当前终端访问官方源、BMCLAPI 均遇到 DNS 解析失败，真实源计划生成尚未验证。
Python 3.12 的最低版本验证及跨平台实际安装验证仍待完成。
已有虚拟环境时，可以直接验证，避免触发 uv 联网解析：

```sh
.venv/bin/python -m mcinst --help
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

## 结构

| 文件 | 职责 |
| --- | --- |
| `src/mcinst/cli.py` | 命令行参数与展示 |
| `src/mcinst/catalog.py` | 异步查询、来源回退、哈希验证 |
| `src/mcinst/models.py` | 元数据与查询结果模型 |
| `src/mcinst/sources.py` | 来源策略与受限地址映射 |
| `src/mcinst/errors.py` | 可由任意前端展示的错误 |
| `src/mcinst/installation/models.py` | 实例请求、平台、文件和安装计划模型 |
| `src/mcinst/installation/rules.py` | 平台及功能规则判定 |
| `src/mcinst/installation/planner.py` | 文件展开、去重、冲突检查与规划服务 |
| `docs/install-plan.md` | 当前功能边界、数据流与 Review 说明 |
| `docs/roadmap.md` | 开发顺序、验收条件与后续设计 |

核心不依赖界面库。Textual 在接入 TUI 时添加；新职责出现后再拆分目录。

参考：[Mojang 版本清单](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json)、
[BMCLAPI 文档](https://bmclapidoc.bangbang93.com/)、
[HTTPX AsyncClient](https://www.python-httpx.org/async/)。
