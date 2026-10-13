# Garmin / COROS 本地同步

在 Garmin 和 COROS 之间同步运动，支持双向同步、多账号隔离、并发处理和失败重试。
在 macOS 或 Linux 上通过 `run_sync.sh` 运行。

## 安装与配置

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run_sync.sh --init-config
```

编辑生成的 `config.local.json`，填写启用组的账号信息：

- `garmin`：`email`、`password`、`token_salt`、`domain`（`COM` 或 `CN`）。
- `coros`：`email`、`password`。
- `direction`：`garmin_to_coros` 或 `coros_to_garmin`。
- `id`：稳定的账号组标识；单账号也使用一个组。

只启用需要同步的组。真实凭据只保存在已忽略的 `config.local.json`，
`config.example.json` 是空凭据模板。完整字段见 [配置指南](docs/configuration.md)。

## 同步

```bash
./run_sync.sh
./run_sync.sh --list
./run_sync.sh --account account_a
./run_sync.sh --direction coros_to_garmin
./run_sync.sh --headless-only --jobs 2
./run_sync.sh --headless-only --full-scan
```

默认先尝试无头同步；Garmin 认证失败时打开浏览器刷新 token，再重试当前组。
`--headless-only` 不弹浏览器，`--browser-only` 先打开浏览器登录再同步。
需要浏览器登录时自动检查并安装 Playwright 和 Chromium；验证码或 MFA 在浏览器中手工完成。

只刷新登录状态：

```bash
./run_sync.sh --account account_a --refresh-token browser
./run_sync.sh --account account_a --refresh-token password
```

每组的数据库、加密 token 和运动文件保存在 `db/accounts/ID/`。
失败记录会在后续运行中重试；不要删除进度目录，更换邮箱或 Garmin 区域时使用新的组 id。
已有旧配置和进度时，先按配置指南执行迁移。

COROS 同步期间避免在其他设备或网页重新登录，以免当前会话失效。
代理可在 JSON 的 `defaults` 或组内 `options` 中设置 `HTTPS_PROXY` / `HTTP_PROXY`。
登录、网络和上传问题见 [排障指南](docs/troubleshooting.md)。

## 离线测试

```bash
.venv/bin/python -m unittest discover -s scripts/tests -t . -p 'test_*.py'
```

测试使用模拟登录、上传和临时数据库，不执行真实同步。

## 致谢

Garmin 模块源自 [yihong0618/running_page](https://github.com/yihong0618/running_page)。
