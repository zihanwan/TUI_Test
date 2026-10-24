# TUI

```sh
uv sync
uv run mcinst tui --directory instances/.minecraft
```

可追加 `--source official` 或 `--source bmclapi`，默认 auto。
已有虚拟环境可运行 `.venv/bin/mcinst tui`。

## 当前功能

- 下载：版本搜索、正式版/快照筛选、实例命名、来源与平台选择、并发配置。
- 预览：列出文件数及已知大小。确认安装后使用这份计划，不重新查询版本清单。
- 任务：文件进度、下载/复用数量、实际传输字节、日志、取消与失败重试。
- 实例：列出本工具安装记录，包括失败和取消任务；离线校验、按固定计划修复。
- 设置：切换当前会话的游戏目录。默认目录为 instances/.minecraft；启动时可用参数指定。

任务栏在首次安装时显示；没有空闲状态说明。日志默认折叠。
宽终端左右分栏，少于 90 列时上下排列。内容可滚动，Tab 切换控件，方向键选择版本，
Enter 在版本列表中预览计划；`/` 搜索，Esc 返回，Ctrl+R 刷新，Ctrl+Q 退出。
输入框中的 `/` 正常输入。退出活动任务时先取消并等待文件清理，再退出界面。
下载期间可以切换页面，安装配置和目录暂时锁定。

## 实例名称

原版游戏版本与本地实例名分开。例如安装 1.20.1 并命名为“生存世界”：

```text
.minecraft/
    versions/生存世界/
        生存世界.json
        生存世界.jar
    .mcinst/versions/生存世界/
        upstream.json
        install-plan.json
        instance.json
```

公开 JSON 的 id 为“生存世界”，与目录名和 JAR 基名一致。
原始版本 JSON 单独保存，继续使用 Mojang 提供的哈希校验；公开 JSON 作为后处理产物记录哈希。
修复会从已校验的原始 JSON 重新生成公开 JSON，完整文件恢复不需要重新查询元数据。
同一游戏版本可以安装多个名称，assets 和 libraries 在共享根目录内复用。

名称是单个目录组件，支持中文和内部空格；不允许空名称、首尾空白、路径分隔符、
Windows 保留名和结尾句点，最长 64 个字符。大小写或 Unicode 规范化后冲突的名称拒绝安装。
新建实例不能覆盖现有同名目录。恢复和修复需要明确使用已有实例记录。
无名称的旧计划继续按原版版本 ID 读取，不搬动或改写已有实例。

CLI 也可以命名：

```sh
uv run mcinst install 1.20.1 --name '生存世界' --directory instances/.minecraft
uv run mcinst verify instances/.minecraft '生存世界'
```

## Review

| 文件 | 职责 |
| --- | --- |
| `ui/app.py` | Textual 组件、消息、后台任务、进度和取消 |
| `ui/app.tcss` | 布局、颜色与窄终端排列 |
| `ui/service.py` | 前端服务入口、本地实例读取、固定计划预览 |
| `installation/models.py` | 原版版本、实例名称、目标路径 |
| `installation/planner.py` | 命名实例的下载路径 |
| `installation/postprocess.py` | 保留上游 JSON 并生成公开版本 JSON |
| `tests/test_named_instances.py` | 命名、多实例、原始哈希、恢复与预览安装一致性 |
| `tests/test_tui.py` | Textual Pilot 安装、配置失效、网络重试、取消、修复与窄布局 |

UI 不解析上游格式，不直接写游戏文件；安装、校验和下载仍由核心模块完成。
网络任务通过异步 workers 执行，文件扫描和哈希工作在线程中运行。
预览配置变化会取消过期请求并丢弃旧计划；安装期间使用固定配置。
本地修复使用记录的平台和功能条件，不依赖远程版本列表成功加载。

本轮环境无法解析 PyPI 域名，Textual 未安装，也未能重新生成 uv.lock。
111 项核心/服务测试已通过，5 项 Pilot 测试因缺少依赖跳过；不宣称已验证界面启动或布局。
在联网环境运行 uv sync 会更新锁文件并安装依赖，随后应运行：

```sh
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
```

依赖使用 Textual 6.x。Java 选择、加载器安装、登录与启动留在后续阶段。

参考：[workers](https://textual.textualize.io/guide/workers/)、
[Pilot 测试](https://textual.textualize.io/guide/testing/)。
