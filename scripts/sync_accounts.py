import argparse
import asyncio
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
import os
from pathlib import Path
import re
import signal
import sys
import time
from typing import TextIO
from urllib.parse import quote, quote_plus, urlsplit

try:
    from .account_config import (
        DEFAULT_STATE_ROOT, DIRECTIONS, Account, config_path, initialize_config,
        load_accounts, worker_environment,
    )
    from .account_state import lock_state
    from .config_migration import DEFAULT_LEGACY_FILE, migrate_configuration
    from .runtime_setup import prepare_runtime
    from .utils.sync_progress import PROGRESS_PREFIX
except ImportError:
    from account_config import (
        DEFAULT_STATE_ROOT, DIRECTIONS, Account, config_path, initialize_config,
        load_accounts, worker_environment,
    )
    from account_state import lock_state
    from config_migration import DEFAULT_LEGACY_FILE, migrate_configuration
    from runtime_setup import prepare_runtime
    from utils.sync_progress import PROGRESS_PREFIX


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def log(message: str, stream: TextIO | None = None) -> None:
    output = stream if stream is not None else sys.stdout
    if not output.isatty():
        message = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(message, file=output, flush=True)


def worker_command(mode: str, refresh_token: str | None = None) -> list[str]:
    if refresh_token:
        script = "generate_garth_token_browser.py" if refresh_token == "browser" else "generate_garth_token.py"
        return [sys.executable, str(PROJECT_ROOT / "scripts" / "garmin" / script)]
    return ["bash", str(PROJECT_ROOT / "run_sync.sh"), *([] if mode == "default" else [mode])]


def redact(line: str, env: dict[str, str]) -> str:
    private = []
    for key, value in env.items():
        if not value:
            continue
        if key in {"GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMIN_TOKEN_SALT", "COROS_EMAIL", "COROS_PASSWORD"}:
            private.append(value)
        elif key.lower().endswith("proxy"):
            private.append(value)
            try:
                proxy = urlsplit(value)
                private.extend(item for item in (proxy.username, proxy.password) if item)
            except ValueError:
                pass
    variants = {part for value in private for part in (value, quote(value, safe=""), quote_plus(value), *value.splitlines()) if part}
    for value in sorted(variants, key=len, reverse=True):
        line = line.replace(value, "[REDACTED]")
    line = re.sub(r"[\w.+-]+@[\w.-]+", "[REDACTED]", line)
    return re.sub(
        r"(?i)((?:[\w-]*token[\w-]*|password|secret|authorization|email|username)[\"']?\s*[:=]\s*).+",
        r"\1[REDACTED]", line,
    )


@contextmanager
def worker_output(
    account_id: str, env: dict[str, str], stream: TextIO | None = None,
    interactive: bool = True,
) -> Iterator[Callable[[str], None]]:
    output = stream if stream is not None else sys.stdout
    terminal = interactive and output.isatty()
    progress_active = False
    width = 0

    def forward(line: str) -> None:
        nonlocal progress_active, width
        safe_line = redact(line.rstrip(), env)
        message = f"[{account_id}] {safe_line}"
        if terminal and safe_line.startswith(PROGRESS_PREFIX):
            output.write("\r" + message.ljust(width))
            output.flush()
            width = len(message)
            progress_active = True
        else:
            if progress_active:
                output.write("\n")
                progress_active = False
                width = 0
            log(message, output)

    try:
        yield forward
    finally:
        if progress_active:
            output.write("\n")
            output.flush()


def forward_worker_output(
    lines: Iterable[str], account_id: str, env: dict[str, str], stream: TextIO | None = None,
) -> None:
    with worker_output(account_id, env, stream) as forward:
        for line in lines:
            forward(line)


async def run_worker(
    account: Account, mode: str, refresh_token: str | None = None, interactive: bool = True,
) -> int:
    env = worker_environment(account, state_root=DEFAULT_STATE_ROOT)
    if refresh_token:
        env["GARMIN_BROWSER_HEADLESS"] = "1" if mode == "--headless-only" else "0"
        env["GARMIN_BROWSER_CI_MODE"] = env["GARMIN_BROWSER_HEADLESS"]
    env["PYTHON_BIN"] = sys.executable
    env["PYTHONUNBUFFERED"] = "1"
    env["SYNC_PROGRESS_TTY"] = "1" if interactive and sys.stdout.isatty() else "0"
    worker = await asyncio.create_subprocess_exec(
        *worker_command(mode, refresh_token), env=env, cwd=PROJECT_ROOT,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True, umask=0o077,
    )
    try:
        with worker_output(account.id, env, interactive=interactive) as forward:
            while line := await worker.stdout.readline():
                forward(line.decode("utf-8", errors="replace"))
        return await worker.wait()
    except BaseException:
        try:
            os.killpg(worker.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            # Drain pipes while waiting so buffered output cannot prevent shutdown.
            await asyncio.wait_for(worker.communicate(), timeout=10)
        except asyncio.TimeoutError:
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await worker.communicate()
        raise


async def run_accounts(
    accounts: list[Account], mode: str, refresh_token: str | None, jobs: int,
) -> int:
    semaphore = asyncio.Semaphore(jobs)
    interactive = min(jobs, len(accounts)) == 1

    async def run_account(account: Account) -> int:
        async with semaphore:
            started = time.monotonic()
            log(f"[{account.id}] 开始 {account.direction}")
            try:
                with lock_state(account, DEFAULT_STATE_ROOT):
                    exit_code = await run_worker(account, mode, refresh_token, interactive)
            except Exception as error:
                exit_code = 1
                detail = str(error) if isinstance(error, ValueError) else type(error).__name__
                log(f"[{account.id}] 无法同步：{detail}", sys.stderr)
            log(f"[{account.id}] {'成功' if exit_code == 0 else '失败'}")
            log(f"[{account.id}] 账号总耗时：{time.monotonic() - started:.2f}s")
            return exit_code

    tasks = [asyncio.create_task(run_account(account)) for account in accounts]
    try:
        results = await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Queued tasks can finish cancellation before active workers finish cleanup.
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    succeeded = sum(code == 0 for code in results)
    failed = len(accounts) - succeeded
    log(f"同步汇总：成功 {succeeded} 组，失败 {failed} 组")
    return 1 if failed else 0


def positive_jobs(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--jobs 必须是正整数。") from None
    if jobs < 1:
        raise argparse.ArgumentTypeError("--jobs 必须是正整数。")
    return jobs


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="多进程并行同步 Garmin / COROS，独立进程与状态目录。")
    result.add_argument("--config", help="多账号 JSON 配置路径，默认 config.local.json")
    result.add_argument("--account", help="仅运行指定的已启用账号组 id")
    result.add_argument("--direction", choices=DIRECTIONS, help="覆盖所选组的同步方向")
    result.add_argument("--jobs", type=positive_jobs, default=4, help="同时运行的账号组数，默认 4；设为 1 恢复串行")
    result.add_argument("--full-scan", action="store_true", help="完整扫描历史运动，忽略增量边界和 GARMIN_NEWEST_NUM")
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--headless-only", action="store_true", help="只跑无头同步")
    mode.add_argument("--browser-only", action="store_true", help="先打开浏览器刷新 token，再同步")
    action = result.add_mutually_exclusive_group()
    action.add_argument("--list", action="store_true", help="仅列出组 id、状态和方向，不打印凭据")
    action.add_argument("--init-config", action="store_true", help="创建权限为 0600 的多账号配置，不覆盖已有文件")
    action.add_argument("--migrate-legacy", metavar="ID", help="迁移旧账号配置和进度到指定组，私有归档旧配置，不执行同步")
    action.add_argument("--refresh-token", choices=("browser", "password"), help="仅为所选组刷新 token，不同步运动")
    result.add_argument("--legacy-config", help="仅迁移时指定旧环境配置文件")
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.legacy_config and not arguments.migrate_legacy:
            raise ValueError("--legacy-config 必须配合 --migrate-legacy。")
        if arguments.migrate_legacy:
            backup = migrate_configuration(
                arguments.legacy_config or DEFAULT_LEGACY_FILE,
                config_path(os.environ, arguments.config), arguments.migrate_legacy,
                DEFAULT_STATE_ROOT, PROJECT_ROOT / "db",
            )
            print(f"[{arguments.migrate_legacy}] 配置和进度已迁移，旧配置私有归档至 {backup.parent.name}；未执行同步。")
            return 0
        if arguments.init_config:
            initialize_config(config_path(os.environ, arguments.config))
            print("已创建 config.local.json，请在本地填写账号组。")
            return 0
        accounts = load_accounts(arguments.config)
        if arguments.list:
            for account in accounts:
                print(f"{account.id}: {'启用' if account.enabled else '停用'}，{account.direction}")
            return 0
        selected_id = arguments.account
        selected = [account for account in accounts if account.enabled and (not selected_id or account.id == selected_id)]
        if not selected:
            raise ValueError("没有可运行的账号组，或指定组不存在/未启用。")
        mode = "--headless-only" if arguments.headless_only else "--browser-only" if arguments.browser_only else "default"
        if arguments.direction:
            selected = [replace(account, direction=arguments.direction) for account in selected]
        selected = [replace(account, env={**account.env, "SYNC_FULL_SCAN": "1" if arguments.full_scan else "0"})
                    for account in selected]
        prepare_runtime(browser_required=False)
        return asyncio.run(run_accounts(selected, mode, arguments.refresh_token, arguments.jobs))
    except FileExistsError:
        print("配置已存在，未覆盖。", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        detail = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(f"配置错误：{detail}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断同步。", file=sys.stderr)
        return 130
    except RuntimeError as error:
        print(f"运行准备失败：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
