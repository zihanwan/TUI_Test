# 单文件下载：Review 说明

本文件描述第 3 阶段的单文件传输层：把一个 Artifact 下载到指定 Minecraft 根目录。
目前已接入并发队列、安装状态和原版实例组装，CLI 提供 install / verify。
完整使用说明见 [Vanilla 安装](vanilla-install.md)；Range 字节级续传仍待后续实现。

## Review 顺序

| 文件 | 职责 |
| --- | --- |
| `downloads/models.py` | 进度、结果及尝试失败记录 |
| `downloads/retry.py` | 重试条件、指数退避及 Retry-After |
| `downloads/files.py` | 根目录、路径检查及已有文件的哈希复用 |
| `downloads/transfer.py` | 传输、校验、临时文件及原子提交 |
| `downloads/errors.py` | 下载错误、目标占用及来源耗尽错误 |
| `tests/test_downloads.py` | 网络和文件系统故障验证 |

## 接口

```python
from pathlib import Path

import httpx

from mcinst.catalog import CatalogService
from mcinst.downloads import ArtifactDownloader
from mcinst.installation.models import InstanceSpec, TargetPlatform
from mcinst.installation.planner import InstallPlanService


async def download_one_file():
    spec = InstanceSpec(
        minecraft_version="1.20.1",
        target=TargetPlatform(os="linux", arch="x86_64", os_version="6.0"),
    )
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        plan = await InstallPlanService(CatalogService(client)).plan(spec)
        downloader = ArtifactDownloader(client)
        result = await downloader.download(
            plan.artifacts[0],
            Path("instances/demo/minecraft"),
        )
        return result
```

调用者创建并关闭 HTTPX 客户端，可通过客户端配置代理、超时和连接池。
此例只下载一个计划文件，不会组装出完整实例；后续调度器将复用同一个下载器。
输入 Artifact 在开始时重新验证并复制，界面后续修改原模型不影响已开始的任务。

## 传输与提交

1. 解析调用者选择的根目录，检查目标路径，拒绝根目录以下的符号链接和非普通文件目标。
2. 已有文件若匹配计划 SHA-1 和已声明大小，返回 reused，不请求网络。
3. 每次尝试使用同目录下独占创建的 `.mcinst-*.part` 临时文件。
4. 流式读取响应并增量计算哈希；默认每块 64 KiB，内存不缓存整个文件。
5. 校验计划大小和哈希，flush / fsync，关闭文件。
6. 再次检查目标路径，通过 os.replace 原子提交。

已有文件大小一致但没有预期哈希时，仍重新下载；只比较大小不能确认内容相同。
下载或校验失败时保留原有目标文件，删除本次拥有的临时文件。
下载器不清理其他任务或先前进程留下的 `.part` 文件。
取消异常原样传播，临时文件在 finally 中同步清理。

大文件的已有内容校验通过线程执行；流式写盘和最终 flush / fsync / replace
使用同步文件操作。本轮目标为单文件正确性，未声称所有磁盘操作都不阻塞事件循环。
后续并发调度需结合实际磁盘性能评估 I/O 工作线程。
原子替换防止消费者读取到半个文件，不等于整个实例的事务，也不保证断电后的目录持久性。

## 重试与来源回退

默认每来源最多尝试 2 次，按 Artifact.candidates 顺序执行，不自行增加来源。
因此固定来源计划不会隐式访问其他来源。

| 情况 | 行为 |
| --- | --- |
| 408、425、429、500、502、503、504 | 有限同源重试，耗尽后换源 |
| HTTP 404 等其他状态 | 跳过同源重试，直接尝试下一个候选 |
| 网络传输失败、内容解码失败 | 有限同源重试，耗尽后换源 |
| 大小或 SHA-1 不符 | 清理本次临时文件，有限重试后换源 |
| 未请求却收到 206 Partial Content | 拒绝发布文件，尝试下一来源 |
| 本地磁盘、权限或路径错误 | 立即失败，不消耗网络重试 |
| 任务取消、调用者进度回调错误 | 传播给调用者，不视为来源故障 |

退避默认从 0.5 秒开始，最大等待预算 10 秒。Retry-After 支持整数秒和 HTTP 日期。
若服务端要求等待超过预算，跳过该来源，不提前重试；固定来源则返回失败。
策略参数可通过 RetryPolicy 配置，重试次数及等待时间都有上限。
所有来源耗尽后 DownloadFailed.failures 保留每次尝试的来源、序号、原因及状态码。

本轮每次失败后从零重试，不发送自己的 Range 请求，不拼接不同来源的残片。
跟随下载源的重定向，最终响应须为 HTTPS；请求偏好 identity 编码。
若响应仍使用 HTTP 压缩，则按解码后的文件内容校验，不能把压缩后的 Content-Length
当成原始文件大小。非压缩响应同时检查 Content-Length 与实际大小。

## 结果与进度

DownloadResult 的 status 为 downloaded 或 reused；只有 SHA-1 验证通过的已有文件可 reused。
verification 为 sha1、size 或 none，说明本次对照了哪些计划信息。
即使返回实际计算的 SHA-1，没有预期哈希时也不会标为 sha1 验证。
size 是最终文件大小，不包含失败尝试消耗的流量；attempts 为本次来源请求尝试数，
不单独计入重定向请求。reused 的 attempts 为 0，source 为 null。

可选同步回调 on_progress 接收 DownloadProgress：相对路径、来源、总尝试序号、
本次已下载字节和预计总字节。每次尝试从 0 开始，未知总量为 null。
回调只报告传输字节，不表示校验或安装完成；以返回 DownloadResult 为成功依据。
回调应快速返回；TUI 后续可在回调中向事件队列发送消息。

单个下载器在同一事件循环内拒绝同时写同一个目标，返回 DownloadBusy。
跨下载器、跨进程的实例锁由安装执行层负责；本模块不提供全局文件锁。
路径检查不承诺抵御其他进程同时替换目录树的恶意竞态，根目录应由调用者控制。

## 验证与下一步

本模块有 26 项离线测试；最新全项目测试数见 README，Ruff 与格式检查通过。
覆盖 502 换源、固定源边界、部分传输失败、损坏内容、响应压缩、重定向、
Retry-After、取消、磁盘失败、符号链接、已有文件复用及进度回调异常。
当前会话拒绝了提升沙箱网络权限的申请，真实源下载尚未验证。

下载器返回成功只表示该文件完成；安装器在全部下载与后处理成功后才标记实例已安装。

参考：[HTTPX 异步流式接口](https://www.python-httpx.org/async/)、
[HTTP Retry-After](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)。
