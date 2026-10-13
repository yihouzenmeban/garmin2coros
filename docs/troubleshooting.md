# Garmin 同步日志排障

## Logfire 返回 401 / Invalid token

`Logfire API returned status code 401` 和 `Failed to export span batch`
来自 Garth 的遥测上报，不是 Garmin 登录接口的响应，也不能据此判断运动同步失败。
项目使用的 Garth 0.7.10 默认启用遥测，会在导入时初始化 Logfire。

项目通过 `scripts/garmin/garth_auth.py` 的 `load_garth()` 统一加载 Garth：

- 在导入前将 `GARTH_TELEMETRY_ENABLED` 和 `GARTH_TELEMETRY_SEND_TO_LOGFIRE` 设为 `false`。
- 对已经初始化的 Garth 客户端也关闭遥测。
- 同步客户端和密码登录生成 token 的入口均使用这个加载函数。

不需要创建 Logfire token，也不需要删除数据库、删除保存的 Garmin 会话或更换登录凭据。
遥测配置参考 [Garth 官方文档](https://garth.readthedocs.io/en/latest/telemetry/)。

## 首次访问时提示 Garmin 未登录

新进程启动时尚未恢复 `db/accounts/ID/garth_token.enc`，不代表已保存的 token 失效。
这个正常初始化步骤仅记录调试日志；保存的 token 解密或验证失败仍记录警告，
密码登录或浏览器登录失败仍抛出异常，不会因本次日志修复而被忽略。

## 定时同步报 SSLError，稍后手动运行成功

这说明登录阶段的 HTTPS 通信失败，不能仅凭 `SSLError` 判断密码或 token 已失效。
Garmin 登录遇到连接错误、超时或 TLS `UNEXPECTED_EOF_WHILE_READING` 时，
会在底层请求重试之后，再分别等待 60 秒、180 秒重试登录，最多尝试 3 次。
重试仍使用当前账号状态，账号锁在等待期间保持持有；不会从头重跑已上传的运动。
证书校验失败、其他未知 TLS 错误及账号认证错误不参与这种延迟重试，
认证失效仍走原有认证恢复流程。最终失败返回非零退出码，保留同步进度。

后台日志中的账号开始、子进程输出、结束和汇总均带本地时间及 UTC 偏移，
例如 `[2026-10-13T10:15:00+08:00]`。SSL 日志记录已识别的原因码及允许的
Garmin/S3 目标域名，不输出原始异常、URL 路径或查询参数、请求头和凭据。
可以据此区分证书错误与连接提前关闭，并核对重试是否恢复；未识别的错误仍仅记录类型。
手动和自动同步使用相同的登录重试策略，无需调整 launchd 的每日执行时间。

## 提示同步完成，但运动没有同步

旧版只查询 `is_sync_coros = 0` 的记录。一旦上传异常将状态设为 `2`，
后续运行便永久跳过这条记录，可能直接以退出码 `0` 结束。
另外，旧版 COROS 上传异常会调用 `exit()`，AWS 上传异常只打印日志，
都会让上层无法可靠识别失败。

现在同步入口会：

- 同时选择状态 `0`（待同步）和 `2`（上次失败），每次最多处理 1000 条。
- 即使本次 Garmin 返回空列表，也继续处理数据库中待同步或失败的记录。
- 根据 COROS 账号区域选择对应的 OSS bucket，并使用实际上传返回的对象路径。
- 只有 OSS 上传成功且 COROS 明确接受导入，才把状态设为 `1`。
- 保留失败记录供下次重试；单条失败不跳过其他记录。
- 输出获取条数、待同步条数、逐条结果和最终成功/失败计数。
- 下载、上传或导入失败时返回非零退出码，不再把失败当成完成。

直接使用原有启动方式重新运行即可，无需删除数据库、删除 token 或手动修改状态。
状态 `1` 的记录仍默认跳过，避免重复导入已同步运动。

失败日志会指出所处阶段。COROS 拒绝导入时，会输出 HTTP 状态或业务
`result` / `status`，但不打印完整请求、响应、密码或访问 token。
修复了重试与错误传播逻辑，并不等于真实账号上的导入已经成功；
应以重新运行后的逐条结果和 COROS 中的实际运动为准。

## 上传阶段出现 JSONDecodeError

旧 STS 地址 `https://faq.coros.com/openapi/oss/sts` 当前返回 HTTP 404 和 HTML，
旧代码直接解析响应便会报 `JSONDecodeError`。这一步尚未开始上传运动文件。

项目现在采用 COROS 官方 Training Hub 的上传流程：

- 根据登录返回的区域选择 `training.coros.com`、`trainingcn.coros.com` 或 `trainingeu.coros.com`。
- 请求对应站点的 `/api/proxy/oss/sts`，携带当前 COROS 登录 token 和区域 Cookie。
- 先检查 HTTP 状态，再校验业务返回码、解码凭据并检查必需字段。
- 使用 STS 返回的 `Region` 和 `Bucket` 初始化 OSS/S3，而非固定服务器区域。
- 导入 COROS 时使用实际上传的 bucket，保持上传和导入路径一致。

接口和字段依据
[COROS 官方上传组件](https://staticcn.coros.com/coros-traininghub-v2/assets/index-DYlaTbLn.js)。
正常同步入口会自动传递 COROS 会话，无需新增环境变量、手动获取 STS token，
也不再需要旧的 `app_id` / `sign` 参数。
直接调用 `AliOssClient` 或 `AwsOssClient` 时，应传入已登录 COROS 客户端的
`access_token` 和 `region_id`。

若新请求返回 401，日志会明确显示 `COROS STS request failed: HTTP 401`，
应检查 COROS 登录会话是否有效或是否被其他设备登录替换；
不要因此刷新 Garmin token 或删除数据库。
错误日志不会输出 Cookie、临时密钥、完整响应或解码后的凭据。

## 浏览器登录后下载 OAuth 参数时报 TLS EOF

`thegarth.s3.amazonaws.com/oauth_consumer.json` 的 `SSLEOFError` / `UNEXPECTED_EOF_WHILE_READING`
表示下载 Garth OAuth 参数时 HTTPS 连接被提前关闭。这不是账号绑定问题，也不是
COROS 运动上传失败；不能据此判断密码错误或 MFA 验证失败。
具体原因可能是临时网络中断、代理或服务端连接异常，需结合当前网络检查。

浏览器生成 token 现在先检查这份参数，再开始 Garmin 登录和 MFA：

- TLS/连接错误、超时及 HTTP 429/500/502/503/504 最多请求三次，间隔 1 秒、2 秒。
- 重试耗尽后，使用现有 Playwright 无头 Chromium 访问同一 HTTPS 地址，沿用当前组的代理。
- 不关闭证书验证，不改用 HTTP 或第三方镜像，不把 OAuth 参数写入配置或提交到 Git。
- 检查 HTTP 状态、JSON 和两个必需字段；失败只输出安全的原因与网络检查提示。
- 参数预检失败时不启动账号登录、不覆盖已有 token，也不修改运动同步进度。

如果 Requests 和 Chromium 都失败，需要恢复这台机器到该 S3 地址的 HTTPS 连通性。
若使用代理，在 `config.local.json` 的 `defaults` 或 `account_b.options` 中填写实际
`HTTPS_PROXY`（必要时同时设置 `HTTP_PROXY`），并检查 `NO_PROXY` 是否让 Requests 绕过代理。
配置中的空代理字符串会取消继承系统代理；端口以自己的代理客户端为准，不要填写猜测的端口。
无需删除数据库或更换账号 id。

恢复网络或更新代码后，可以只重试 B 组，避免再次处理 A 组：

```bash
./run_sync.sh --account account_b --browser-only
```

出现 MFA 提示时在浏览器中完成验证。只有实际逐条同步结果和 COROS 中的运动记录
才能确认同步成功；参数下载成功不等于账号登录或运动上传成功。

## 本地离线验证

```bash
.venv/bin/python -m unittest discover -s scripts/tests -t . -p 'test_*.py' -v
```

`-t .` 将测试作为项目包加载，确保同步模块按项目结构导入。
遥测测试在独立进程中检查导入时的配置；登录和上传使用模拟客户端，
重试与同步入口测试使用临时 SQLite 数据库，不访问账号、不上传运动，
也不修改真实数据库或加密 token。
