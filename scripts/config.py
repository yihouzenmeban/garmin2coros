from collections.abc import Mapping
import os
from pathlib import Path
import re
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT_ID = os.environ.get("SYNC_ACCOUNT_ID", "")
if ACCOUNT_ID and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", ACCOUNT_ID):
    raise ValueError("Invalid sync account ID.")
ACCOUNT_DIR = Path(os.environ.get("SYNC_STATE_ROOT") or PROJECT_ROOT / "db" / "accounts") / ACCOUNT_ID
GARMIN_FIT_DIR = str(ACCOUNT_DIR / "garmin-fit" if ACCOUNT_ID else PROJECT_ROOT / "garmin-fit")
COROS_FIT_DIR = str(ACCOUNT_DIR / "coros-fit" if ACCOUNT_ID else PROJECT_ROOT / "coros-fit")
DB_DIR = str(ACCOUNT_DIR if ACCOUNT_ID else PROJECT_ROOT / "db")
GARTH_TOKEN_FILE = os.path.join(DB_DIR, "garth_token.enc")

RUNTIME_ENV_KEYS = {
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
    "https_proxy", "http_proxy", "no_proxy",
    "GARMIN_LOGIN_MODE", "GARMIN_BROWSER_HEADLESS", "GARMIN_BROWSER_CI_MODE",
    "GARMIN_BROWSER_TIMEOUT_SECONDS",
}


def load_config_values(env: Mapping[str, str] | None = None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    if os.environ.get("SYNC_ACCOUNT_WORKER") != "1" or not os.environ.get("SYNC_ACCOUNT_ID"):
        raise ValueError("请使用 run_sync.sh 从 config.local.json 选择账号组运行。")
    return dict(os.environ)


def apply_runtime_env(env: Mapping[str, str] | None = None) -> None:
    source = load_config_values(env)
    runtime_source = {key: os.environ[key] for key in RUNTIME_ENV_KEYS if key in os.environ}
    values = {**runtime_source, **source}
    for key in RUNTIME_ENV_KEYS:
        if key in values:
            os.environ[key] = values[key]


def load_sync_config(
    defaults: Mapping[str, str | int], env: Mapping[str, str] | None = None,
) -> dict[str, str | int]:
    apply_runtime_env(env)
    values = load_config_values(env)
    return {key: values.get(key) or default for key, default in defaults.items()}


def dispatch_cli(direction: str | None = None, refresh_token: str | None = None) -> None:
    if os.environ.get("SYNC_ACCOUNT_WORKER") == "1":
        if not os.environ.get("SYNC_ACCOUNT_ID"):
            raise ValueError("账号子进程缺少 SYNC_ACCOUNT_ID。")
        return
    arguments = []
    if direction:
        arguments.extend(["--direction", direction])
        if not any(mode in sys.argv for mode in ("--headless-only", "--browser-only")):
            arguments.append("--headless-only")
    if refresh_token:
        arguments.extend(["--refresh-token", refresh_token])
    python = PROJECT_ROOT / ".venv" / "bin" / "python"
    executable = str(python) if python.is_file() else sys.executable
    os.execv(executable, [executable, str(PROJECT_ROOT / "scripts" / "sync_accounts.py"),
                          *arguments, *sys.argv[1:]])


if __name__ == "__main__":
    dispatch_cli()
