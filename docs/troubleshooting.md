# 同步排障

## 登录或下载时出现 SSL / TLS 错误

`SSLError`、`SSLEOFError` 或 `UNEXPECTED_EOF_WHILE_READING` 表示 HTTPS 通信失败。
先检查网络与代理；不能仅凭这些错误判断密码或 token 已失效。

Garmin 登录遇到连接错误、超时或 TLS EOF 时，会在底层请求重试后，
分别等待 60 秒、180 秒重试登录，最多尝试 3 次。证书校验失败和账号认证错误不参与这种延迟重试。
重试保留当前账号锁与同步进度，最终失败返回非零退出码。

后台日志带本地时间及 UTC 偏移，可用来核对重试时间和恢复情况。
日志只记录已识别的网络原因、允许的目标域名或异常类型，不输出请求凭据。

代理在 `config.local.json` 的 `defaults` 或组内 `options` 设置 `HTTPS_PROXY` / `HTTP_PROXY`。
检查 `NO_PROXY` 是否意外绕过代理；配置空字符串会取消继承系统代理。

## 浏览器登录需要验证码或 MFA

使用可见浏览器为指定账号组登录：

```bash
./run_sync.sh --account account_a --browser-only
```

在浏览器中完成验证后，脚本继续同步。仅需更新登录状态时使用：

```bash
./run_sync.sh --account account_a --refresh-token browser
```

`--headless-only` 不等待人工接管。新进程启动时尚未恢复加密 token 属于正常初始化；
实际解密或验证失败才会记录警告。每组 token 位于 `db/accounts/ID/garth_token.enc`。

## 获取 Garmin OAuth 参数失败

若错误指向 `thegarth.s3.amazonaws.com/oauth_consumer.json`，失败发生在登录前的参数预检。
脚本先通过 HTTP 客户端重试，耗尽后尝试用无头 Chromium 访问同一地址，并沿用当前组代理。

两种方式均失败时，恢复到该地址的 HTTPS 连通性，再重试当前组。
参数预检失败不会覆盖已有 token 或同步进度，无需删除数据库、更换账号 id 或关闭证书校验。

## 同步结束但运动没有出现

查看最后的成功、失败计数和错误摘要；进度达到 100% 只表示本批次处理结束。
只有上传和导入成功才记录为已同步。待同步及失败记录会在后续运行重试，单批最多处理 1000 条。
重新运行指定组：

```bash
./run_sync.sh --account account_a --headless-only
```

较早日期补录的运动可能位于增量扫描范围之外，需要立即补齐时执行：

```bash
./run_sync.sh --account account_a --headless-only --full-scan
```

以同步汇总和目标平台的实际运动记录确认结果。不要删除进度数据库或手工重置已同步状态。
扫描范围和批次规则见 [配置指南](configuration.md)。

## COROS 上传凭据或导入失败

上传凭据由当前 COROS 登录会话向所在区域的 Training Hub 获取。
出现 `COROS STS request failed: HTTP 401` 时，检查会话是否失效或被其他设备登录替换。
同步期间避免在其他设备或网页重新登录 COROS。

HTTP、JSON 解析或凭据字段错误会终止本次上传并保留失败记录，修复网络或登录问题后重试即可。
错误摘要中的 `result` / `status` 用于判断 COROS 是否接受导入；无需手动获取 STS 密钥或刷新 Garmin token。

## Logfire 401 / Invalid token

`Logfire API returned status code 401` 来自 Garth 遥测，不是 Garmin 登录响应。
项目在加载 Garth 前关闭遥测，也会关闭已初始化客户端的遥测。
如果仍出现此提示，确认使用项目的 `run_sync.sh` 和 `.venv` 运行。
无需创建 Logfire token，也无需为此删除同步数据库或登录会话。
