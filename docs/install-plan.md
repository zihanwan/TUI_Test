# InstallPlan：Review 说明

用户选择 Minecraft 版本及目标平台后，`mcinst plan` 解析安装所需的文件和后处理步骤。
本阶段只读取必要的 JSON 元数据，不下载客户端、库、声音、纹理等内容，也不创建实例目录。

## Review 顺序

1. `src/mcinst/installation/models.py`：输入、平台、文件及计划的数据契约。
2. `src/mcinst/installation/rules.py`：平台和功能规则语义。
3. `src/mcinst/installation/planner.py`：纯规划函数及网络编排服务。
4. `src/mcinst/catalog.py`、`src/mcinst/sources.py`：资源索引校验与文件来源路由。
5. `tests/test_plan.py`：固定样本及异常路径。

## 调用入口

```sh
uv run mcinst plan 1.20.1 --os linux --arch x86_64 --files
uv run mcinst plan 1.20.1 --os windows --arch x86_64 --os-version 10.0 --json
uv run mcinst plan 1.20.1 --os osx --arch arm64 --source official --json
```

网络不可用时可先运行离线测试：

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

上面的版本号用于演示命令形式，不意味着对应的所有平台组合已完成真实源验证。
输出 JSON 到文件可由调用方自行重定向；CLI 不主动写计划文件。

## 数据流

`InstallPlanService.plan(spec)` 使用 CatalogService 获取版本详情，再读取资源索引。
版本详情按总清单的 SHA-1 校验；资源索引按详情声明的 SHA-1 和大小校验。
所有输入就绪后交给 `build_plan(spec, detail, index, policy)`。
后者只做内存计算，可单独用于 UI 和离线测试，不依赖 HTTP 客户端或 Textual。
直接调用纯函数时，调用者负责先完成输入元数据的内容校验。

版本 JSON 自身也在下载计划中，记录原始字节大小、哈希和规范上游地址。
后续执行器若重新下载它，必须验证同一个哈希，不能在安装中途重新解析“最新”元数据。

## 输出契约

计划 schema_version 当前为 1。所有路径相对于实例的 Minecraft 根目录，采用 `/` 分隔。

| 字段 | 含义 |
| --- | --- |
| `spec` | 游戏版本、目标 OS / 架构 / 系统版本、功能条件 |
| `artifacts` | 版本 JSON、客户端、库、原生库、资源索引、资源对象、日志配置 |
| `native_extractions` | 旧式原生库归档及目标目录、上游排除前缀 |
| `asset_copies` | 资源对象至 virtual 或 resources 布局的映射 |
| `java_requirement` | 上游声明；未提供则 null，本阶段不选择或下载 Java |
| `diagnostics` | 来源回退、校验信息缺失、未验证兼容性等信息 |
| `declared_library_count` | 上游库声明数，尚未进行平台过滤 |
| `known_download_bytes` | 去重后所有已知大小文件的合计，不是缓存扣除后的剩余流量 |
| `unknown_size_count` | 大小未声明的文件数量 |

每个 Artifact 包含 kind、path、upstream_url、candidates、sha1 和 size；
候选地址列表 `candidates` 按来源策略排列。
执行器应先准备并校验文件，再执行解压与映射步骤；当前没有执行器。

## 平台与路径规则

- OS 使用 windows、linux、osx；架构使用 x86、x86_64、aarch64。
  amd64 / x64 归一为 x86_64，arm64 归一为 aarch64。
- 不存在 rules 时允许；存在规则时初始禁止，最后一条匹配规则决定结果。
  OS 名称、架构、系统版本和 features 条件共同决定匹配。
- 未传入的 feature 默认为 false；未知规则字段或无法解析的表达式会报错。
- 系统版本和架构表达式使用 Python `re.fullmatch`；未实现完整 Java 正则方言。
  同机 macOS 使用产品版本，而不是 Darwin 内核版本；跨系统不猜架构和系统版本。
- 旧式 natives 根据 OS 取 classifier，`${arch}` 展开成 32 或 64。
  ARM64 若 classifier 未明确包含 arm64 / aarch64，直接报不支持。
- 现代 Maven 原生分类器按上游库规则筛选，保留为下载文件，不生成旧式解压步骤。
- Maven 坐标支持 group:artifact:version[:classifier][@extension]。
  缺少 downloads 的旧库按仓库和坐标生成路径；没有可靠哈希和大小时保留 null 并告警。
- 同一资源哈希只下载一次；同目标不同内容、大小冲突、大小写冲突、文件/目录冲突均报错。
- 路径拒绝绝对路径、`..`、反斜杠、盘符、Windows 保留名称等不可移植组件。
  执行器仍须在写入和解压时检查真实路径、符号链接及归档成员，规划检查不能替代这些检查。

## 来源与支持边界

auto 为 BMCLAPI 优先，官方回退；固定来源只给出对应候选。
原版下载按已知主机映射：资源对象走 `/assets`，依赖库走 `/maven`，
Mojang 元数据和客户端文件保留上游路径。历史 HTTP 文件地址升级为同主机 HTTPS。
镜像选择不改变文件目标路径、大小和哈希。未知主机拒绝规划，不进行全局文本替换。

当前支持独立原版和带 assetIndex 的资源布局，包含 virtual、map_to_resources 映射。
缺少 assetIndex 的历史格式、inheritsFrom、JAR 重定向及加载器安装尚不支持。
客户端、资源索引和日志配置要求有哈希及大小；旧式库的信息缺失会明确展示。
平台筛选只证明匹配上游元数据，不代表本工具已验证 Java 或原生二进制可以运行。

## 验证记录

43 项离线测试和 Ruff 检查通过，CLI 文本与 JSON 两种展示已测试。
固定样本位于 `tests/fixtures/`，为合成测试数据，不是真实可下载版本。
网络编排测试验证只请求版本清单、详情、资源索引三个文档；任何游戏内容请求都会失败测试。
官方源和 BMCLAPI 的真实计划生成尝试均受当前终端 DNS 解析失败影响，尚未通过。
Python 3.12 最低版本运行与跨平台实际安装验证仍待补充。

参考实现核对：[minecraft-launcher-lib 安装流程](https://github.com/JakobDev/minecraft-launcher-lib/blob/master/minecraft_launcher_lib/install.py)。
下载源接口：[BMCLAPI 文档](https://bmclapidoc.bangbang93.com/)。
