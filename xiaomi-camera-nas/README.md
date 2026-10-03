# 远程下载录像

访问地址：`http://<机器名>.<tailnet>.ts.net/`

手机或远程电脑安装 Tailscale，登录与这台 Mac 相同的账号并连接，然后打开上述完整域名。服务仅在 Tailscale 私人网络内提供访问。

点击录像下载压缩版，再用本地播放器打开。页面也提供“下载原文件”链接（`?original=1`）。压缩版为最大 1280×720、HEVC（H.265）、15 帧/秒、视频码率上限 400 kbps、AAC 音频 32 kbps。400 kbps 是上限：画面静止时编码器只用约 180 kbps，画面变化多时才接近上限。降低分辨率、帧率和码率会损失细节；原录像始终保留。实测 60 秒夜间样片从 4.5 MiB 降到 1.6 MiB，完整文件大小随内容变化。

## 写入检测与压缩

1. 每 15 秒观察文件大小、mtime、ctime、inode 和设备号。必须连续观察 **5 分钟无变化**才排队压缩；每次变化重新计时。重启后未处理文件也重新观察完整的 5 分钟。
2. 创建私有临时快照，复制时计算 MD5，并检查路径和已打开文件的状态。复制完成后独立读取源文件计算 MD5，与快照比对。FFmpeg 只读取快照。
3. 一次只压缩一个文件，使用 Mac VideoToolbox 编码器和较低进程优先级。压缩中约每 2 秒检查源文件状态；若再次写入，终止压缩并清理临时结果，重新等待。
4. 压缩完成后再次核对源文件 MD5，验证输出时长、末尾解码和体积，再原子更新清单。清单记录 `source_md5`、源文件状态、输出文件名、大小和时长。

普通复制、MD5 和本服务的进程锁不能与不配合的 NAS 写入端建立严格的原子事务。这是乐观校验：发现并发写入或内容不一致就放弃结果并重试。5 分钟安静期也不能保证未来不再补写。MD5 用于检测非恶意文件变化，不作为防篡改认证。

## 下载一致性

- 下载开始前重新计算源文件 MD5。发现变化，旧压缩结果失效，返回 HTTP 503 的等待页面并重新开始 5 分钟观察；请稍后刷新目录再下载。
- 校验通过后打开不可变的压缩文件，整个下载读取同一个文件句柄。源录像此后发生变化或生成新压缩版，也不会混入本次下载。已经开始的下载可能是旧快照，但文件内部保持一致。
- 响应使用 `Content-Disposition: attachment`。不提供浏览器在线播放、版本重定向或断点续传；忽略 Range，始终返回 HTTP 200 的完整文件，下载失败后重新下载，避免新旧版本拼接。
- 目录列表只检查元数据，不对所有录像计算 MD5。实测本机校验 128 MiB 文件约需 0.25 秒，每次下载开始时会有这项开销。

## 文件与运行

- 原录像：`/Users/Shared/XiaomiCamera`
- 下载服务：`.remote-access/server.py`，本机监听 `127.0.0.1:8765`
- 压缩与一致性检查：`.remote-access/playback.py`
- 无写入等待时间：`SETTLE_SECONDS = 5 * 60`
- 运行时产生的文件全部在 `.remote-access/var/`，整个目录被 `.gitignore` 排除，可随时删除（删除压缩缓存后会重新压缩）：
  - 压缩副本、清单、锁和临时文件：`var/playback/`
  - 日志：`var/log/server.log`、`var/log/server-error.log`、`var/log/downloads.jsonl`（及轮转备份 `.1`–`.3`）
- 自启动：`~/Library/LaunchAgents/com.xiaomicamera.video-server.plist`

总磁盘占用仍包含原文件和压缩副本；处理期间额外需要一份源文件快照及正在生成的压缩文件。正常完成或取消会清理快照，成功发布会删除该录像被替换的旧播放副本。异常断电可能留下临时文件，暂不自动清理已删除原录像对应的副本。

使用本机 Elmedia Player 内的 `/Applications/Elmedia Player.app/Contents/Resources/ffmpeg`，请保留此应用；也可将带 VideoToolbox 编码器的 FFmpeg 安装到服务的 PATH 中。

Mac 必须开机、联网、保持唤醒并连接 Tailscale。录像服务在安装 LaunchAgent 的用户登录 macOS 后自动启动，退出该用户会停止服务。

服务已启用下载版和诊断日志。以后修改代码需重启后生效；请在没有下载或压缩任务时执行：

```sh
launchctl kickstart -k gui/501/com.xiaomicamera.video-server
```

## 下载诊断日志

独立日志：`.remote-access/var/log/downloads.jsonl`，每行一个 JSON 对象。单文件最多约 5 MiB，保留 3 份轮转备份，时间采用 UTC（北京时间加 8 小时）。

```sh
tail -f /Users/Shared/XiaomiCamera/.remote-access/var/log/downloads.jsonl
```

- `request_id`：关联同一次请求的开始、进度和结束；响应也带 `X-Request-ID`。
- `event`：`download_start`、`download_progress`、`request_end`、`server_start`。
- `verify_seconds`：压缩副本有效性检查耗时，包含源文件 MD5 校验。
- `headers_seconds`：从处理请求到响应头发送结束的时间。
- `file_bytes_expected`、`file_bytes_sent`：视频预期字节数和成功交给 socket 的字节数，不包括响应头、目录页面。
- `transfer_seconds`、`total_seconds`：视频发送阶段和请求处理总耗时。
- `send_MiB_s`：发送阶段累计平均速度，单位 MiB/s，包含磁盘读取和 socket 等待。
- `outcome`：`sent_to_proxy` 已全部发送给下游 socket；`disconnected` 下游连接断开；`not_ready` 视频尚未就绪；`source_truncated` 读取提前结束；`error` 出错；`head_only` 仅查询响应头。

在持续完成写入时，每约 5 秒记录一次进度；如果 socket 写入阻塞，进度日志也会暂停。中断时当前失败写入的块可能已有部分送出，`file_bytes_sent` 是保守计数。进程被强制终止或断电时可能只有开始日志，没有结束日志。

**这些指标不能证明远端下载完成。** Tailscale Serve 通过本机回环代理访问服务，`peer` 通常是 `127.0.0.1`，发送速度包含代理缓冲影响，并非实际公网或手机落盘速度。远端实际测速仍需客户端的下载耗时和字节数；连接是否直连或中继可在远端下载时运行：

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale status
```

日志不记录 Cookie、授权头或完整查询参数，仅记录路径和是否下载原文件。

运行测试：

```sh
python3 .remote-access/test_playback.py
```

测试覆盖 5 分钟观察、写入导致重新计时、复制/压缩期间继续写入、元数据相同而 MD5 不同、压缩失败及截断结果不可发布、处理中止、附件下载、拒绝过期结果，以及下载中替换压缩文件仍读取同一份内容。

停止远程访问：

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --http=80 off
```

恢复远程访问：

```sh
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg --http=80 http://127.0.0.1:8765
```
