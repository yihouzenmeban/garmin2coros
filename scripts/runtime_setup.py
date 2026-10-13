from contextlib import contextmanager
import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES = ("garth", "requests", "urllib3", "oss2", "boto3", "cryptography", "requests_oauthlib")


def install(arguments: list[str]) -> None:
    environment = dict(os.environ)
    environment.update(GARTH_TELEMETRY_ENABLED="false", GARTH_TELEMETRY_SEND_TO_LOGFIRE="false")
    try:
        result = subprocess.run([sys.executable, *arguments], cwd=PROJECT_ROOT,
                                env=environment, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("依赖安装超过 300 秒，请检查网络后重试。") from None
    except OSError:
        raise RuntimeError("无法启动依赖安装，请检查 Python 环境。") from None
    if result.returncode:
        raise RuntimeError("依赖安装失败，请检查 Python 环境及网络后重试。")


def chromium_available() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            return False
        try:
            browser = playwright.chromium.launch(headless=True, timeout=10000)
            browser.close()
        except Exception:
            return False
        return True


@contextmanager
def installation_lock():
    directory = PROJECT_ROOT / "db"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory / ".runtime-setup.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def prepare_runtime(browser_required: bool = False) -> None:
    started = time.monotonic()
    dependencies = (*DEPENDENCIES, "playwright") if browser_required else DEPENDENCIES
    if browser_required or any(importlib.util.find_spec(name) is None for name in dependencies):
        with installation_lock():
            # Another account may have installed the dependencies while this one waited.
            importlib.invalidate_caches()
            if any(importlib.util.find_spec(name) is None for name in dependencies):
                print("[sync] 安装缺失依赖（所有账号共用一次）", flush=True)
                arguments = ["-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")]
                install([*arguments, "playwright"] if browser_required else arguments)
                importlib.invalidate_caches()
            if browser_required and not chromium_available():
                print("[sync] 安装缺失的 Playwright Chromium", flush=True)
                install(["-m", "playwright", "install", "chromium"])
    print(f"[sync] 运行准备耗时：{time.monotonic() - started:.2f}s", flush=True)
