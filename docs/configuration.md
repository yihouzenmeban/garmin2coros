# 配置与运行参考

只维护一套多账号 JSON：单账号也是 `accounts` 中的一组。
空凭据模板 `config.example.json` 可提交；本地 `config.local.json` 已忽略，权限为 `0600`。
普通运行不读取旧 `.env` 或单账号环境变量。
配置缺失、损坏或凭据不完整时直接报错，不自动回退。

首次安装和初始化见 [README](../README.md)。初始化不覆盖已有文件；
填写账号后，只启用需要运行的组。不要通过命令行传入密码或提交本地 JSON。

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

基础运行命令见 [README](../README.md)，下列参数可按需组合：

| 参数 | 用途 |
| --- | --- |
| `--account ID` | 只处理指定的已启用账号组 |
| `--jobs N` | 账号组并发数，默认 4；设为 1 时逐组处理 |
| `--headless-only` | 不弹浏览器 |
| `--browser-only` | 先使用可见浏览器刷新 token，再同步 |
| `--refresh-token browser` / `password` | 仅刷新 token，不同步运动 |
| `--full-scan` | 完整扫描历史运动，忽略增量边界和数量限制 |
| `--config PATH` | 指定账号 JSON 文件 |
| `--list` | 只列组 id、启停状态和固定同步方向，不输出凭据 |

`--headless-only` 与 `--browser-only` 互斥。默认运行方式见 README；
网络、OSS 或 COROS 错误直接返回失败并保留进度，不触发 Garmin 浏览器登录。
仅刷新登录状态的示例：

```bash
./run_sync.sh --account account_a --refresh-token browser
./run_sync.sh --account account_a --refresh-token password
```

`--refresh-token` 可组合 `--headless-only`，不能与 `--list`、初始化或迁移操作同时使用。
配置文件也可通过 `SYNC_ACCOUNTS_FILE` 指定；读取优先级为显式 `--config` >
`SYNC_ACCOUNTS_JSON` > 配置文件。

## 扫描与启动

首次运行、没有扫描记录或距上次完整扫描满 7 天时扫描全部历史运动。
日常增量扫描在连续两页全部属于已知活动时停止；异常或损坏的扫描记录触发完整扫描。
旧日期补录的活动可能在增量窗口之外，会在下一次完整扫描时被发现；
需要立即同步时运行 `./run_sync.sh --headless-only --full-scan`。
设置 `GARMIN_NEWEST_NUM` 为正数时主动限制扫描窗口，需用 `--full-scan` 补齐较早的历史记录。
扫描与上传进度独立；未上传和失败记录仍从数据库读取，每批最多处理 1000 条。
已有运动记录和同步状态会保留。

启动时集中检查一次同步所需的 Python 包，普通同步与密码方式刷新 token 不检查或启动 Chromium。
仅在实际需要浏览器登录时检查 Playwright 和 Chromium，缺失时安装；
有待同步记录时才加载 COROS 所在地区的上传 SDK。

日志显示准备、模块加载、各账号及同步阶段耗时。并发阶段的“累计”是各任务耗时之和，
可能超过实际经过时间；模块加载耗时单独报告。网络与认证失败的处理见 [排障指南](troubleshooting.md)。

## 并发与进度

不同账号组默认多进程并行，最多同时运行 4 组；一组结束后立即启动下一组。
用 `--jobs N` 指定账号组并发数（正整数），`--jobs 1` 恢复逐组串行。
每组保持独立登录会话、数据库和上传凭据，运动同步及 token 刷新都使用此调度方式。
按 Ctrl+C 会终止正在运行的账号进程组（包括浏览器子进程），释放状态锁，
不再启动等待中的组，退出状态为 130。

每组默认并行处理 4 条活动。在 `defaults` 统一设置，或在单组 `options` 中覆盖：

```json
{"SYNC_UPLOAD_WORKERS": 4, "SYNC_DOWNLOAD_WORKERS": 4}
```

`SYNC_UPLOAD_WORKERS` 设为 1 恢复组内串行处理，与 `--jobs` 控制的账号组并发数独立。
实际下载并发不超过两个并发参数中的较小值。将 `SYNC_DOWNLOAD_WORKERS` 设为 1
可串行下载并保留上传并发。下载会话彼此独立，数据库写入由主线程串行处理。
带宽或服务端限流可能限制实际速度；失败记录仍会保留供后续重试。

同步时显示类似下面的进度：

```text
[account_b] 同步进度：[########------------] 40/100 (40%) 成功 39 失败 1
```

只运行一组或使用 `--jobs 1` 时，终端原地刷新同一行，最多每 0.2 秒更新一次；
多组并行时，每跨过 5% 按行输出带账号前缀的快照，避免互相覆盖。
重定向到文件或管道时同样每跨过 5% 输出一个快照，避免刷屏。
“完成”包括已记录状态的成功和失败，100% 不代表全部成功；最后显示成功/失败汇总，
失败原因以分类摘要显示。

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
