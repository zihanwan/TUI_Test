# PCL / HMCL 共享游戏目录

本项目以传统 Minecraft 游戏根目录为兼容基线：一个 `.minecraft` 包含多个版本，
客户端 JSON 与 JAR 位于各自的版本目录，assets 与 libraries 共享。
`--directory` 直接指向这个根目录，不会为每个实例创建 `.minecraft`。

## 目录约定

```text
instances/.minecraft/
    assets/
        indexes/
        objects/
    libraries/
    versions/
        1.20.1/
            1.20.1.json
            1.20.1.jar
        1.21.1/
            1.21.1.json
            1.21.1.jar
    .mcinst/
        versions/
            1.20.1/
                instance.json
                install-plan.json
            1.21.1/
                instance.json
                install-plan.json
    .mcinst.lock
```

JSON 与 JAR 的基名、所在版本目录名、JSON 的 id 相同。
默认使用上游版本 ID，也可用 --name 指定实例名。命名后目录、JSON 的 id 与 JAR 基名同步修改；
原版版本 ID 保留在固定计划中。同一游戏版本可用不同名称安装多份。
单个实例名不能对应多个不同平台或功能组合。
命名和原始元数据的处理见 [TUI 说明](tui.md)。
安装记录位于 `.mcinst` 内，不是启动器需要解析的游戏描述文件。
安装其他版本不会覆盖前一个版本的计划和状态。

## 安装与校验

```sh
.venv/bin/mcinst install 1.20.1 --directory instances/.minecraft --source official
.venv/bin/mcinst install 1.21.1 --directory instances/.minecraft --source official
.venv/bin/mcinst verify instances/.minecraft 1.20.1
.venv/bin/mcinst verify instances/.minecraft 1.21.1
```

示例默认本机平台；跨平台下载需显式指定 OS / 架构。
只有一个受管理版本时，verify 的实例名参数可省略；多个版本时必须明确选择。
verify 仅检查本工具记录的版本，不自动接管其他启动器安装的版本。
同名目录已存在且没有本工具记录时拒绝覆盖，其他已有版本和用户文件保留。

不同版本的库与资源按现有下载器的哈希检查复用，无须额外复制。
若两个受管理版本要求同一相对路径却具有不同内容，安装前报冲突，避免修复一个版本时破坏另一个。
整个游戏根目录使用进程锁，同一根目录内的安装和迁移串行执行。
这里的共享限于同一个根目录；跨根目录的全局内容缓存尚未实现。

## 在 PCL、HMCL 中添加

添加 `instances/.minecraft` 为游戏文件夹，再刷新版本列表，即可按约定扫描 versions 下的版本。
程序不写入 PCL、HMCL 的私有配置，是否开启版本隔离由启动器决定。
开启版本隔离后，mods、config、saves 等通常位于 `versions/<id>/`；共享 assets、libraries 的位置不变。
当前下载器不负责启动，不会主动迁移用户的存档和模组到隔离目录。
已有旧实例中的运行数据迁移时保留其相对位置，开启隔离前需由用户或启动器管理这些数据。

格式依据来自两者的官方文档，实际启动器识别与启动仍待手动验收。
现有真实示例仍为 Linux / x86_64，布局迁移不会改变原生库平台。
Prism 原生实例格式、官方启动器 profile 及整合包导出需要独立适配。

## 迁移旧独立实例

兼容此前的 `旧实例/minecraft` 与 `旧实例/.minecraft` 两种源布局：

```sh
.venv/bin/mcinst migrate-layout instances/vanilla-1.20.1-linux \
  --directory instances/.minecraft
.venv/bin/mcinst verify instances/.minecraft 1.20.1
```

迁移同时锁住源与目标，预检符号链接、同名版本及文件冲突。
可合入已经包含其他版本的共享根目录；共享文件内容一致时复用，不同则在搬动前报错。
同一文件系统内使用重命名移动文件，无需重新下载；暂不支持跨文件系统迁移。
空目录也保留。计划原始字节、哈希、状态和所有额外用户文件均保留。
目标版本的记录改为根目录布局 `.`，源游戏树搬空后删除。
旧外层只保留原计划、原记录、锁及 migration-target.json 作为迁移凭据，可在验收后自行清理。
对旧外层 verify 会提示改用共享根目录，重复原迁移命令是无操作。

迁移日志 `.mcinst/migration.json` 在搬动之前原子保存。
若磁盘失败或进程中断，应重跑同一条迁移命令，按日志补完剩余移动和元数据提交。
未完成迁移期间拒绝 install / verify，不会静默将半迁移文件视为完整安装。
不要手动删除迁移日志，也不要在迁移期间由其他启动器修改目录。

现有真实 1.20.1 在迁移前后均校验 3631 个受管理文件，缺失、不匹配和无预期哈希均为 0。
固定计划 SHA-256 与客户端 JAR inode 保持一致。

## 下载并发

默认总并发 16、每逻辑来源并发 8，仍支持 1～64 的显式覆盖：

```sh
.venv/bin/mcinst install 1.20.1 --directory instances/.minecraft \
  --os linux --arch x86_64 --os-version 6.0 \
  --concurrency 24 --per-source 12 --source official
```

数值表示异步请求并发，不是磁盘线程数，不保证固定的速度提升倍数。

## 官方依据

- [HMCL 版本隔离文档](https://docs.hmcl.net/launcher/isolation.html)：展示 `.minecraft` 下共享 assets、libraries 和 versions 的布局，以及各版本独立的运行数据。
- [PCL Wiki：替换标记](https://github.com/Meloong-Git/PCL/wiki/%E6%9B%BF%E6%8D%A2%E6%A0%87%E8%AE%B0)：`minecraft` 是游戏根目录，`version_path` 为根目录下的 versions/版本名；隔离目录随开关选择版本目录或根目录。
- [XMCL folder.ts](https://github.com/voxelum/minecraft-launcher-core-node/blob/master/packages/core/folder.ts)：版本 JSON 与 JAR 分别为 versions/id/id.json、versions/id/id.jar。
