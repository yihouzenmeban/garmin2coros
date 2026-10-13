# 仓库贡献指南

## 项目结构

本项目在 macOS / Linux 上将 Garmin 运动同步到 COROS。

- `run_sync.sh`：启动入口；`scripts/sync_accounts.py`：多账号进程调度。
- `scripts/garmin/`：认证、运动下载和同步编排；`scripts/coros/`：登录与导入。
- `scripts/oss/`：区域上传；`scripts/utils/`：通用工具。
- `scripts/account_config.py`、`scripts/account_state.py`：配置与账号状态管理。
- `scripts/tests/`：离线测试；`docs/`：配置与排障文档。

安装与基本运行见 [README](README.md)，完整配置见 [配置指南](docs/configuration.md)。

## 开发与验证

在仓库根目录执行以下命令，使用 README 中创建的 `.venv`：

```bash
.venv/bin/python -m unittest discover -s scripts/tests -t . -p 'test_*.py'
.venv/bin/python -m unittest scripts.tests.test_account_config -q
bash -n run_sync.sh
```

第一条运行完整测试，第二条是单模块测试示例，第三条检查 Shell 语法。项目没有独立构建步骤，
也未配置格式化或静态检查工具。根据改动先运行最快的相关检查。

## 代码风格

新 Python 代码使用四空格缩进，函数与模块采用 `snake_case`，类采用 `PascalCase`。
修改旧模块时保持周围缩进与命名风格，避免无关格式调整。优先使用类型标注、明确的错误处理
和有界网络超时；仅为不明显的意图添加注释。不要凭空添加接口、配置或路径。

## 测试约定

使用 `unittest`、`unittest.mock` 和临时文件或数据库，不执行真实登录或上传。
测试文件命名为 `test_*.py`，方法命名为 `test_<行为>`。每个行为改动阶段先补充测试并运行，
实现后再运行相关测试；当前没有覆盖率数值门槛。不要添加仅证明已删除功能不存在的墓碑测试，
但应保留验证现有安全、接口或持久化约束的负向测试。

## Git 与合并请求

编辑前运行 `git status`，用 3–6 条要点说明计划。保留用户已有改动，提交前审查 diff 并完成
相关验证，只暂存本任务的文件或代码块。每次修改文件的任务都应以 Git 提交结束；
只读任务或无文件改动时不创建空提交。

提交到当前分支（目前为 `main`），沿用 Conventional Commits 类型前缀和中文说明，
例如 `docs: 整理仓库文档`。未经明确要求，不推送或改写历史。合并请求说明问题、改动后行为、
验证结果及相关 issue；任务完成时报告文件列表、验证结果和提交哈希。

## 安全与配置

不得在代码、日志或提交中暴露密码、token、私钥或真实账号信息；`config.example.json` 的凭据保持为空。
本地配置与状态目录规则见 [配置指南](docs/configuration.md)。修改状态或并发逻辑时，保持账号隔离、
状态锁和失败重试约定。未经要求，不添加遥测、分析或额外网络调用。
