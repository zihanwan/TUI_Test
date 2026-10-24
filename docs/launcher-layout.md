# 启动器兼容布局

本项目采用传统 Minecraft 游戏根目录布局作为兼容基线。
没有一个实例描述格式能让所有启动器自动识别；游戏文件布局、游戏运行目录和启动器实例元数据是不同层次。

## 文件应该放在哪里

```text
instances/vanilla-1.20.1-linux/
    instance.json
    install-plan.json
    .minecraft/
        assets/
        libraries/
        versions/
            1.20.1/
                1.20.1.json
                1.20.1.jar
```

`1.20.1.json` 与 `1.20.1.jar` 同放在版本目录 `versions/1.20.1/`，
两者基名、版本目录名和 JSON 的 id 相同。这部分在旧布局中已经正确，无须移动到实例最外层。
assets 和 libraries 位于游戏根目录，与 versions 并列。
原生库及旧版资源映射继续使用固定计划中的相对路径。

新实例的游戏根目录命名为 `.minecraft/`；此前的 `minecraft/` 也是有效的游戏根目录名称。
目录名称本身不决定兼容性，关键是第三方启动器将哪一层作为其游戏文件仓库。
用户看到的 `--directory` 是本工具管理的实例外层，其中的 instance.json 是本工具的状态记录。
它不是 Mojang 的版本 JSON，也不是通用的第三方启动器实例描述文件。

## 在其他启动器中使用

对于支持添加 Minecraft 游戏文件夹的启动器，添加本实例内的 `.minecraft/`，
然后刷新本地版本列表；不要把含有 instance.json 的外层当成游戏根目录。
旧布局未迁移时应选择 `minecraft/`。install 的结果和 verify 输出会显示实际游戏根目录。

启用版本隔离时，启动器可能把 saves、mods、config 等运行数据放到版本目录。
这不意味着 assets 和 libraries 也必须移入版本目录，版本 JSON 与 JAR 的相对位置仍相同。
最终能否启动还取决于目标系统、Java 及该启动器的解析和参数处理。
当前示例是 Linux / x86_64 目标，改名不会把原生库转换为 macOS / ARM64。

Prism 的文档与源码显示其共享 assets、libraries、meta 等数据，并有自己的实例管理。
其游戏目录解析支持 minecraft 与 .minecraft 两种名称；但仅把目录更名不会生成一个完整的 Prism 原生实例。
Prism、官方启动器配置文件和整合包导出仍需各自的适配，当前不声明已完成这些适配。

## 旧实例迁移

新版本能继续读取没有布局字段的旧记录，默认将其解释为 minecraft/。
旧实例不会在 install 或 verify 时被偷偷搬动，可离线显式迁移：

```sh
.venv/bin/mcinst migrate-layout instances/vanilla-1.20.1-linux
.venv/bin/mcinst verify instances/vanilla-1.20.1-linux
```

迁移持有实例进程锁，将 minecraft/ 更名为 .minecraft/，再原子更新 instance.json 的
minecraft_directory 字段。游戏文件内容和 install-plan.json 原始字节保持不变。
正常写记录失败时回滚目录名；进程若在重命名后、记录提交前中止，重跑迁移命令可补完记录。
目标与源目录同时存在时拒绝合并，遇到符号链接也拒绝操作。
重复迁移已使用 .minecraft/ 的实例是无操作，不再次搬动或下载文件。

本次已迁移现有 1.20.1 实例，迁移前后均校验 3631 个受管理文件：
缺失 0、不匹配 0、无预期哈希 0。固定计划 SHA-256 和客户端 JAR inode 保持一致。
这验证了数据完整性和零重拷贝，不代表已在第三方启动器执行了启动测试。

## 下载并发

默认总并发从 8 调整为 16，每逻辑来源并发从 4 调整为 8。
CLI、安装器和底层队列共用 defaults，仍保留 1～64 的限制，并支持显式覆盖：

```sh
.venv/bin/mcinst install 1.20.1 \
  --directory instances/another-instance \
  --os linux --arch x86_64 --os-version 6.0 \
  --concurrency 24 --per-source 12 --source official
```

这是异步请求并发上调，不是把 Python 的磁盘工作线程数设为该值，也没有宣称确定的速度提升倍数。

## 依据

- [XMCL MinecraftFolder 文档](https://xmcl.app/en/core/core/MinecraftFolder)：以 Minecraft root 为基础的路径接口。
- [XMCL folder.ts](https://github.com/voxelum/minecraft-launcher-core-node/blob/master/packages/core/folder.ts)：
  getVersionRoot / getVersionJson / getVersionJar 明确使用 versions/id/id.json 与 id.jar。
- [Prism 数据位置](https://prismlauncher.org/wiki/getting-started/data-location/)：共享 assets、libraries、meta 与 instances 的结构。
- [Prism MinecraftInstance.cpp](https://github.com/PrismLauncher/PrismLauncher/blob/develop/launcher/minecraft/MinecraftInstance.cpp)：
  gameRoot() 在 minecraft 与 .minecraft 之间解析游戏目录。
