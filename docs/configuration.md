# 统一账号配置

只维护一套多账号 JSON：单账号也是 `accounts` 中的一组。
空凭据模板 `config.example.json` 可提交；本地 `config.local.json` 已忽略，权限为 `0600`。
普通运行不读取旧 `.env` 或单账号环境变量。
配置缺失、损坏或凭据不完整时直接报错，不自动回退。

## 新用户

```bash
./run_sync.sh --init-config
```

编辑生成的本地文件填写账号，启用需要运行的组；初始化不覆盖已有文件。
不要通过命令行传入密码，不要提交本地 JSON。

## 已有账号迁移

先停止使用旧 `db/` 的其他进程，再执行一次：

```bash
./run_sync.sh --migrate-legacy account_a
```

迁移读取原 `.env.garmin.local`，将账号、区域、token 盐、代理及浏览器参数写入 JSON，
并将旧数据库与加密 token 复制到指定组。原数据库不修改，不触发登录或上传。
旧凭据文件退出仓库，私有归档到仓库父目录下的 `.garmin-sync-coros-config-backup-*`。
备份目录权限为 `0700`、凭据文件权限为 `0600`，仅用于回滚，不参与运行。

已有 JSON 时，目标组必须存在且身份匹配；目标状态目录有数据时拒绝覆盖。
复制 token 时必须沿用旧盐且能够解密。失败时保留旧配置，不留下部分新配置。
没有旧数据库时只迁移账号配置，并创建身份绑定。

迁移前确认旧进度确实属于原配置的这对账号。不要先修改旧邮箱以绕过身份检查。
可用 `--legacy-config PATH` 显式指定待迁移旧文件；旧格式解析器只服务迁移命令。
不迁移而直接使用空进度，可能重新处理全部历史运动。

## JSON 字段

- `accounts`：至少一个账号组，可复制模板添加更多组。
- `id`：稳定标识，1–64 位字母、数字、下划线或连字符，首位为字母或数字。
  不允许重复（不区分大小写）；已有登录或运动数据时，邮箱和区域更换需使用新 id。
- `enabled`：布尔值，默认 `true`；停用组可保留空凭据，不可被单独运行。
- `garmin`：`email`、`password`、`token_salt`、`domain`（`COM` 或 `CN`）。
- `coros`：`email`、`password`。
- `defaults` / 每组 `options`：公共与组内参数；组内值优先，不允许放账号密码。
  支持 `GARMIN_NEWEST_NUM`（非负整数）、`GARMIN_LOGIN_MODE`（`browser` 或 `password`）、
  `GARMIN_BROWSER_TIMEOUT_SECONDS`（正整数）、浏览器布尔参数及 HTTP/HTTPS/NO_PROXY 参数。
  `SYNC_UPLOAD_WORKERS`、`SYNC_DOWNLOAD_WORKERS` 为 1–8 的整数，默认均为 4。
  前者控制 Garmin→COROS 同时处理的活动数，后者限制其中的下载并发数。
  `GARMIN_NEWEST_NUM` 为 0 时不限制扫描数量，正整数限制 Garmin 扫描窗口；
  数据库中已发现的待同步和失败记录仍会重试，`--full-scan` 忽略此数量限制。

固定从 Garmin 同步到 COROS，无需配置方向。现有 JSON 中的 `direction: "garmin_to_coros"`
仍可读取，其他方向值会报错，需先确认配置。

JSON 中双引号与反斜杠按 JSON 规则转义，不执行 shell 展开。
系统代理可以沿用，也可在 `defaults` / `options` 中覆盖或用空字符串取消。
组内凭据不能被旧系统账号变量覆盖，子进程不继承全局 `GARTH_TOKEN` / `GARTH_HOME`。

## 运行与 token

```bash
./run_sync.sh --list
./run_sync.sh
./run_sync.sh --headless-only --jobs 2
./run_sync.sh --headless-only --full-scan
./run_sync.sh --account account_b --headless-only
./run_sync.sh --account account_a --browser-only
./run_sync.sh --account account_a --refresh-token browser
./run_sync.sh --account account_a --refresh-token password
```

默认先无头同步，仅 Garmin 认证失败时使用可见浏览器刷新当前组 token 后重试。
网络、OSS 或 COROS 错误直接返回失败并保留进度，不重新登录和重跑整组。
`--headless-only` 不弹浏览器；`--browser-only` 先可见浏览器刷新 token 再同步。
`--refresh-token` 仅刷新 token，不同步运动，可组合 `--headless-only`。
`--list` 只列 id、启停状态和固定同步方向，不输出凭据。
`--config PATH` 指定 JSON，也可用环境变量 `SYNC_ACCOUNTS_FILE`；
优先级为显式 `--config` > `SYNC_ACCOUNTS_JSON` > 配置文件。

旧 Python 同步和 token CLI 也会先转入同一个调度器，支持 `--account`；
不会再用旧根目录进度或从 `.env` 登录。
同步 CLI 默认无头执行；token 浏览器 CLI 默认使用可见浏览器。

## 扫描与启动耗时

首次运行、没有扫描记录或距上次完整扫描满 7 天时扫描全部历史运动。
日常增量扫描在连续两页全部属于已知活动时停止；每组记录 Garmin 活动的完整扫描时间，
只有活动列表获取及批量保存成功后才更新记录。异常或损坏的扫描记录触发完整扫描。
旧日期补录的活动可能在增量窗口之外，会在下一次完整扫描时被发现；
需要立即同步时运行 `./run_sync.sh --headless-only --full-scan`。
设置 `GARMIN_NEWEST_NUM` 为正数时主动限制扫描窗口，需用 `--full-scan` 补齐较早的历史记录。
扫描与上传进度独立；未上传和失败记录仍从数据库读取，每批最多处理 1000 条。
数据库自动增加活动 id 索引，批量查重和保存；已有重复记录和同步状态不删除、不重置。

启动时集中检查一次同步所需的 Python 包，普通同步与密码方式刷新 token 不检查或启动 Chromium。
仅在实际需要浏览器登录时检查 Playwright 和 Chromium，缺失时安装；
多个进程通过共享安装锁协调，等待后重新检查依赖，避免重复安装。
浏览器的可用性仍通过启动并关闭空白浏览器验证，但只在浏览器登录入口执行。
有待同步记录时才加载 COROS 所在地区的上传 SDK。
日志显示运行准备耗时、模块加载耗时、完整/增量/限量扫描模式、各账号总耗时，
以及登录、扫描、查重、下载、上传和导入的阶段耗时。
模块加载耗时单独报告，不计入后续同步阶段的总耗时；延迟加载的 SDK 则计入使用它的阶段。
并发阶段显示各任务耗时之和（“累计”），不能与账号实际经过时间直接相加比较。

COROS HTTP 请求默认连接超时 10 秒、读取超时 30 秒；GET/HEAD 有最多两次重试，
POST 不因读取超时或 HTTP 状态自动重试，避免重复导入。
Garmin API 请求超时 30 秒、最多两次重试；COROS 上传凭据和导入请求使用复用的 HTTP 会话和 10/30 秒超时。
阿里云上传请求超时 30 秒，AWS 使用 10/30 秒超时和有界重试。
浏览器登录使用真实经过时间作为期限，页面导航、填表和轮询计入该期限，结束时关闭浏览器。

## 并发与进度

不同账号组默认多进程并行，最多同时运行 4 组；一组结束后立即启动下一组。
用 `--jobs N` 指定账号组并发数（正整数），`--jobs 1` 恢复逐组串行。
每组保持独立登录会话、数据库和上传凭据，运动同步及 token 刷新都使用此调度方式。
按 Ctrl+C 会终止正在运行的账号进程组（包括浏览器子进程），释放状态锁，
不再启动等待中的组，退出状态为 130。

不修改旧配置也会默认并行处理 4 条 Garmin→COROS 活动。
可在 `defaults` 统一设置，或在单组的 `options` 中覆盖，例如：

```json
{"SYNC_UPLOAD_WORKERS": 4, "SYNC_DOWNLOAD_WORKERS": 4}
```

`SYNC_UPLOAD_WORKERS` 设为 1 恢复组内串行处理，与 `--jobs` 控制的账号组并发数独立。
Garmin 登录与初始 token 恢复在主线程执行；下载线程各自复用从该组 token 创建的独立会话，
不共享或写入登录凭据文件。下载、OSS 上传与 COROS 导入按活动在有界线程池中执行，
最多保留 `SYNC_UPLOAD_WORKERS` 条活动，实际下载并发不超过两个并发参数中的较小值。
将 `SYNC_DOWNLOAD_WORKERS` 设为 1 可串行下载并保留上传并发；数据库状态只由主线程写入。
单批次相同活动 id 只提交一次，避免旧数据库重复记录导致同名文件读写冲突。
阿里云上传线程各自复用独立 HTTP 会话；AWS 客户端在主线程初始化后供上传线程使用，
单文件不再额外创建分片线程，避免并发数量叠加。
阿里云小于 5 MiB 的 ZIP 直接上传，较大文件使用分片上传；失败的分片上传会尝试中止。
AWS 客户端的线程约束见 [Boto3 官方说明](https://docs.aws.amazon.com/boto3/latest/guide/clients.html#multithreading-or-multiprocessing-with-clients)。
带宽或服务端限流可能限制实际速度；失败记录仍会保留供后续重试。

同步时显示类似下面的进度，不再逐条输出成功记录或上传路径：

```text
[account_b] 同步进度：[########------------] 40/100 (40%) 成功 39 失败 1
```

只运行一组或使用 `--jobs 1` 时，终端原地刷新同一行，最多每 0.2 秒更新一次；
最后一条始终更新。多组并行时，每跨过 5% 按行输出带账号前缀的快照，避免互相覆盖。
重定向到文件或管道时同样每跨过 5% 输出一个快照，避免刷屏。
“完成”包括已记录状态的成功和失败，100% 不代表全部成功；最后显示成功/失败汇总，
失败原因合并为最多三类摘要及其余数量，不输出逐条活动 id 或原始异常内容。
总数是本次待处理记录，现有每批最多 1000 条的限制不变。

## 状态与安全

每组使用独立进程与 `db/accounts/ID/`，隔离数据库、加密 token、
`garmin-fit/`。组目录为 `0700`，子进程采用私有文件权限。
`binding.json` 仅保存组 id、邮箱与区域的身份摘要，不保存明文凭据。
同组有并发锁；有数据却没有绑定记录时拒绝运行。
只有绑定格式有效、数据库从未写入运动记录、没有 token、下载文件或未知状态文件时，
修改账号或区域后才会自动重新绑定；这适用于首次登录前留下的空状态。
已有记录（包括删除记录后留下的序号）、token、下载文件，或无法确认状态为空时，
身份不一致仍拒绝运行，必须使用新 id；损坏的绑定不会自动覆盖。
更换密码不影响进度；更换盐后需要重新登录。
一组失败后继续其他组，任何失败返回非零状态；失败记录可重试。

`config.local.*`、`.env`、`.env.*`、`*.env`、整个 `db/`、运动下载目录、
虚拟环境、字节码、缓存、日志和构建产物不进入 Git。
凭据历史曾泄露时仍应轮换密码和会话；忽略规则不能撤回已经泄露的内容。
