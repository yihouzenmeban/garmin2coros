#!/usr/bin/env python3

import os
import sys
from pathlib import Path

CURRENT_DIR = os.path.split(os.path.abspath(__file__))[0]
config_path = CURRENT_DIR.rsplit("/", 1)[0]
project_root = config_path.rsplit("/", 1)[0]
PROJECT_ROOT = Path(project_root)
PROJECT_VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
for path in (config_path, project_root):
    if path not in sys.path:
        sys.path.append(path)

try:
    from config import apply_runtime_env, dispatch_cli, load_config_values
    from garmin.garth_auth import (
        apply_browser_user_agent,
        configure_domain,
        load_garth,
        remove_garth_user_agent,
    )
except ModuleNotFoundError:
    from scripts.config import apply_runtime_env, dispatch_cli, load_config_values
    from scripts.garmin.garth_auth import (
        apply_browser_user_agent,
        configure_domain,
        load_garth,
        remove_garth_user_agent,
    )

REQUIRED_ENV_KEYS = {
    "GARMIN_EMAIL",
    "GARMIN_PASSWORD",
    "GARMIN_TOKEN_SALT",
}


def maybe_reexec_with_project_venv() -> None:
    if os.environ.get("GARMIN_SKIP_VENV_REEXEC") == "1":
        return
    if not PROJECT_VENV_PYTHON.exists():
        return

    current_python = Path(os.path.abspath(sys.executable))
    project_python = Path(os.path.abspath(str(PROJECT_VENV_PYTHON)))
    if current_python == project_python:
        return

    os.environ["GARMIN_SKIP_VENV_REEXEC"] = "1"
    os.execv(
        str(project_python),
        [str(project_python), os.path.abspath(__file__), *sys.argv[1:]],
    )


def load_garmin_env(
    env: dict[str, str] | None = None,
) -> dict[str, str]:
    merged = load_config_values(env)
    config = {
        "GARMIN_AUTH_DOMAIN": str(merged.get("GARMIN_AUTH_DOMAIN", "COM")).strip() or "COM",
        "GARMIN_EMAIL": str(merged.get("GARMIN_EMAIL", "")).strip(),
        "GARMIN_PASSWORD": str(merged.get("GARMIN_PASSWORD", "")),
        "GARMIN_TOKEN_SALT": str(merged.get("GARMIN_TOKEN_SALT", "")),
    }
    missing = sorted(key for key in REQUIRED_ENV_KEYS if not config.get(key))
    if missing:
        raise ValueError(f"Missing required Garmin env: {', '.join(missing)}")
    return config


def resolve_runtime() -> dict[str, object]:
    try:
        from config import GARTH_TOKEN_FILE
        from garmin.garth_token_store import write_encrypted_token
    except ModuleNotFoundError as exc:
        if exc.name not in {"config", "garmin"}:
            raise RuntimeError(
                "缺少 Python 依赖 "
                f"`{exc.name}`。请先运行: python3 -m pip install -r requirements.txt"
            ) from exc
        try:
            from scripts.config import GARTH_TOKEN_FILE
            from scripts.garmin.garth_token_store import write_encrypted_token
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "缺少 Python 依赖 "
                f"`{exc.name}`。请先运行: python3 -m pip install -r requirements.txt"
            ) from exc

    try:
        garth = load_garth()
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 Python 依赖 "
            f"`{exc.name}`。请先运行: python3 -m pip install -r requirements.txt"
        ) from exc

    return {
        "garth": garth,
        "token_path": GARTH_TOKEN_FILE,
        "write_encrypted_token": write_encrypted_token,
    }


def generate_token(
    runtime: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, object]:
    apply_runtime_env(env=env)
    config = load_garmin_env(env=env)
    runtime = runtime or resolve_runtime()

    garth_module = runtime["garth"]
    token_path = Path(str(runtime["token_path"]))
    write_encrypted_token = runtime["write_encrypted_token"]

    existed_before = token_path.exists()
    mtime_before = token_path.stat().st_mtime if existed_before else None

    garmin_domain = configure_domain(garth_module, config["GARMIN_AUTH_DOMAIN"])
    apply_browser_user_agent(garth_module)
    garth_module.login(config["GARMIN_EMAIL"], config["GARMIN_PASSWORD"])
    remove_garth_user_agent(garth_module)

    garth_token = garth_module.client.dumps()
    write_encrypted_token(
        str(token_path),
        config["GARMIN_TOKEN_SALT"],
        garth_token,
    )

    token_exists = token_path.exists()
    mtime_after = token_path.stat().st_mtime if token_exists else None
    updated = token_exists and (not existed_before or mtime_after != mtime_before)

    return {
        "token_path": str(token_path),
        "token_exists": token_exists,
        "token_updated": updated,
        "token_existed_before": existed_before,
        "garmin_domain": garmin_domain,
        "garmin_email": config["GARMIN_EMAIL"],
        "username": getattr(garth_module.client, "username", None),
    }


def main() -> int:
    try:
        maybe_reexec_with_project_venv()
        result = generate_token()
    except Exception as exc:
        print(f"Generate Garmin token failed: {exc}", file=sys.stderr)
        return 1

    print("Generate Garmin token succeeded.")
    print(f"Domain: {result['garmin_domain']}")
    print(f"Email: {result['garmin_email']}")
    print(f"Username: {result['username']}")
    print(f"Token path: {result['token_path']}")
    print(f"Token existed before: {result['token_existed_before']}")
    print(f"Token exists now: {result['token_exists']}")
    print(f"Token updated this run: {result['token_updated']}")
    return 0


if __name__ == "__main__":
    dispatch_cli(refresh_token="password")
    raise SystemExit(main())
