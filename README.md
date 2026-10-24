# mcinst

Python Minecraft Java 版实例下载与安装工具。已实现版本查询、计划预览、原版实例文件安装与离线校验。
支持有界并发、来源回退、文件级恢复、原生库解压和资源映射。
Java 管理、登录、游戏启动、加载器与 TUI 留在后续阶段。

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
uv run mcinst install 1.20.1 --directory instances/vanilla-1.20.1-linux --os linux --arch x86_64 --os-version 6.0 --source official
uv run mcinst verify instances/vanilla-1.20.1-linux
uv run mcinst migrate-layout instances/vanilla-1.20.1-linux
uv run python -m mcinst --help
```

`--source auto` 为 BMCLAPI 优先、官方回退；`official` 和 `bmclapi` 为固定来源。
版本详情先按清单提供的 SHA-1 校验，再解析 JSON，并核对版本 ID。
版本列表沿用上游顺序，不按字符串或 SemVer 重排。未知详情字段会保留。
查询失败退出码为 1；参数错误为 2；用户中断为 130。

默认使用 HTTPX 的环境代理设置，网络超时为 20 秒、连接超时为 10 秒。
CLI 元数据查询当前不提供持久缓存或同源重试；不要把查询成功理解为安装成功。
单文件下载 API 已支持同源重试、来源回退、流式校验及原子提交，
接口和边界见 [下载传输说明](docs/download-transfer.md)。

`install` 创建独立实例目录，新实例游戏文件位于其 `.minecraft/` 子目录。
JSON 与 JAR 同放在 `.minecraft/versions/<版本ID>/`，assets 与 libraries 位于游戏根目录。
第三方启动器添加 Minecraft 游戏文件夹时选择 `.minecraft/`，而非含 instance.json 的实例外层。
旧实例的 minecraft/ 仍可读取；migrate-layout 可离线更名为 .minecraft/。
格式依据和第三方适配边界见 [启动器兼容布局](docs/launcher-layout.md)。
示例明确下载 Linux/x86_64 目标；为本机下载时可省略平台参数。
默认总并发 16、每逻辑来源并发 8，可以通过 `--concurrency` / `--per-source` 调整。
首次保存固定计划，重跑相同命令会复用已校验文件，补齐失败、缺失或损坏的文件与派生资源。
`verify` 离线检查受管理文件。当前支持完整文件恢复，尚不支持 Range 字节级续传。
完整接口、状态和 Review 入口见 [Vanilla 安装说明](docs/vanilla-install.md)。

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

本次开发验证：96 项离线测试、Ruff 检查和格式检查通过，包含安装、恢复和新旧布局迁移。
现有真实 1.20.1 实例的 3631 个文件在布局迁移前后均通过校验，第三方启动器启动尚未验证。
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
| `src/mcinst/downloads/transfer.py` | 单文件下载、流式校验、重试和原子提交 |
| `src/mcinst/downloads/retry.py` | 有限退避及 Retry-After 策略 |
| `src/mcinst/downloads/files.py` | 目标路径检查及已有文件校验 |
| `src/mcinst/downloads/scheduler.py` | 有界任务队列、汇总进度与失败取消 |
| `src/mcinst/downloads/limits.py` | 每逻辑来源并发配额 |
| `src/mcinst/downloads/settings.py` | CLI 与 API 共用的并发默认值 |
| `src/mcinst/installation/installer.py` | 原版安装编排与失败恢复 |
| `src/mcinst/installation/postprocess.py` | 受控原生库解压及资源映射 |
| `src/mcinst/installation/state.py` | 固定计划、状态与进程锁 |
| `src/mcinst/installation/verify.py` | 离线文件校验 |
| `src/mcinst/installation/layout.py` | 新旧根目录解析与离线布局迁移 |
| `docs/install-plan.md` | 当前功能边界、数据流与 Review 说明 |
| `docs/download-transfer.md` | 下载传输模块的接口、故障处理与 Review 说明 |
| `docs/vanilla-install.md` | 完整原版下载、恢复与校验的使用说明 |
| `docs/launcher-layout.md` | 启动器文件结构、目录选择和迁移依据 |
| `docs/roadmap.md` | 开发顺序、验收条件与后续设计 |

核心不依赖界面库。Textual 在接入 TUI 时添加；新职责出现后再拆分目录。

参考：[Mojang 版本清单](https://piston-meta.mojang.com/mc/game/version_manifest_v2.json)、
[BMCLAPI 文档](https://bmclapidoc.bangbang93.com/)、
[HTTPX AsyncClient](https://www.python-httpx.org/async/)。
