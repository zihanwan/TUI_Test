# 开发路线与验收

每个阶段验收后，再推进下一阶段。
账号登录、游戏启动、整合包与服务端安装暂不进入首个版本范围。

当前进度：第 2 阶段 InstallPlan 已实现，等待 Review。
43 项离线测试、Ruff 及格式检查通过；真实下载源在当前终端受 DNS 限制，尚未验证。
下一阶段为下载引擎及原版实例安装。

| 顺序 | 阶段 | 验收结果 |
| --- | --- | --- |
| 1 | 项目骨架与原版元数据 | CLI 可查询版本及详情；来源回退、哈希和 ID 校验有离线测试 |
| 2 | 原版安装计划 | 不下载游戏文件即可列出目标平台需要的客户端、资源、依赖和原生库；确定目标路径及哈希 |
| 3 | 下载引擎与原版实例 | 有界并发、校验、原子提交、基本恢复；实际安装原版并可校验修复 |
| 4 | TUI 最小闭环 | 在 Textual 中选择版本、预览计划、安装与查看错误；后台事件与核心服务解耦 |
| 5 | Fabric / Quilt | 动态查询兼容版本、解析继承并安装；不自动安装或假定存在 API 模组 |
| 6 | Java 管理 | 发现并探测本地 Java、手动指定与自动匹配；安装 Java 和游戏 Java 分别建模 |
| 7 | Forge / NeoForge | 先支持选定的现代格式，执行处理器、检查产物；不支持的格式提前拒绝 |
| 8 | 完善与发布 | Java 下载、跨平台安装验证、故障恢复强化、打包与明确支持矩阵 |

## 第 1 阶段边界

- CLI 和安装核心独立；本阶段仅网络读取，不创建实例。
- 自动模式为国内源优先、官方回退；固定来源不跨源。
- HTTP 错误、损坏 JSON、详情哈希错误均可触发自动回退；保留来源诊断。
- 详情元数据按清单 SHA-1 校验；清单本身依赖所选 HTTPS 来源，不宣称独立真实性验证。
- 原版资源按已知主机及资源类别映射来源；未知主机不自动猜测镜像地址。
- SHA-1 用于上游内容一致性校验，不等价于签名验证。
- 当前不执行 Java、不提供所有版本兼容承诺；第 2 阶段已加入平台库规则解析。

## 第 2 阶段实现

建立 InstanceSpec、TargetPlatform、Artifact、InstallPlan 模型和原版规划器。
计划分两步解析：先固定版本详情，再获取并校验资源索引以展开资源对象。
每个 Artifact 记录规范上游 URL、候选来源、目标相对路径、大小与哈希。
下载源路由不得改变目标路径和预期内容身份。

处理 libraries.rules 的顺序语义、原生库 classifiers、Maven 坐标、
客户端 JAR、资源索引与资源对象；保留旧版本的资源布局分支。
尚未实现的布局应明确报不支持，不能返回一个貌似完整的计划。
计划展示区分上游依赖声明数量和目标平台最终文件数量。
没有 Java 元数据时显示未知，后续兼容规则独立补充，禁止一律默认最新 Java。
支持独立原版、旧式 natives classifiers 和现代分类器文件；继承版本、缺少资源索引的
历史布局及未明确声明 ARM64 的旧式原生库会拒绝规划。
详细行为见 [InstallPlan](install-plan.md)。

## 第 3 阶段设计

采用独立实例目录及程序共享内容缓存。先使用复制；后续评估写时复制。
下载以 .part 写入，校验后原子替换。Range 续传必须校验 Content-Range 和
内容身份，遇到 200 完整响应重新写入；不能确认跨源内容身份时不拼接。
任务状态与安装完成状态分开，单实例加锁，失败保留可用缓存。
修复只触及工具记录的受管理文件，避免覆盖 saves、mods、config 等用户内容。

## 加载器与 Java 的约束

Fabric / Quilt 优先使用各自 Meta profile；继承需要专用合并规则。
Forge / NeoForge 安装包格式、处理器和 Java 约束独立处理。
Java 管理先完成本地发现，再接入需要执行 Java 的安装路径。
处理器执行采用参数数组、受控工作目录、超时、取消及日志捕获；产物校验后才完成。
未知处理器格式不得自行猜测。旧 Forge 在后续通过明确的版本系列逐步扩展。

平台兼容以操作系统、架构、Java、原生库组合为单位记录，
区分“上游存在”“本工具可解析”“实际安装验证”。当前仅开发环境验证不代表跨平台支持。

## 后续模块拆分

随着对应阶段推进，拆分 metadata/、downloads/、installation/、loaders/、
java/、storage/ 和 ui/；避免仅为目录结构预先创建空实现。
核心服务通过结构化事件报告进度；前端负责显示，核心不操作 Textual 控件。

## 参考

- https://bmclapidoc.bangbang93.com/
- https://github.com/FabricMC/fabric-meta/blob/master/README.md
- https://meta.quiltmc.org/
- https://github.com/MinecraftForge/Installer
- https://textual.textualize.io/guide/workers/
- https://minecraft-launcher-lib.readthedocs.io/en/stable/modules/mod_loader.html

后续接入 minecraft-launcher-lib 前，单独验证来源路由、取消、恢复与产物记录能力；
不通过全局 monkey patch 接管第三方网络请求。
