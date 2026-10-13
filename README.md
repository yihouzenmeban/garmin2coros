# Garmin → COROS 本地同步

将 Garmin 运动同步到 COROS，支持多账号隔离、并发处理和失败重试。
在 macOS 或 Linux 上通过 `run_sync.sh` 运行。

## 安装与配置

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
./run_sync.sh --init-config
```

编辑生成的 `config.local.json`，按模板填写账号并启用需要同步的组。
真实凭据只保存在这个已忽略的本地文件中，`config.example.json` 保持为空凭据模板。
字段说明及旧账号迁移见 [配置指南](docs/configuration.md)。

## 同步

```bash
./run_sync.sh --list
./run_sync.sh
./run_sync.sh --account account_a --headless-only
```

默认先尝试无头同步；Garmin 认证失败时打开浏览器刷新 token，再重试当前组。
`--headless-only` 适合不弹浏览器的运行方式。
需要浏览器登录时自动检查并安装 Playwright 和 Chromium；验证码或 MFA 在浏览器中手工完成。

每组的数据库、加密 token 和运动文件保存在 `db/accounts/ID/`。
失败记录会在后续运行中重试；不要删除进度目录。
并发、完整扫描、代理、token 刷新及账号变更规则见 [配置指南](docs/configuration.md)。

登录、网络和上传问题见 [排障指南](docs/troubleshooting.md)。

## 参与开发

项目结构、离线测试命令、代码风格与提交要求见 [仓库贡献指南](AGENTS.md)。

## 致谢

Garmin 模块源自 [yihong0618/running_page](https://github.com/yihong0618/running_page)。
