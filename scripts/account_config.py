from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re

try:
    from . import config as runtime_config
except ImportError:
    import config as runtime_config


DEFAULT_CONFIG_FILE = runtime_config.PROJECT_ROOT / "config.local.json"
EXAMPLE_CONFIG_FILE = runtime_config.PROJECT_ROOT / "config.example.json"
DEFAULT_STATE_ROOT = runtime_config.PROJECT_ROOT / "db" / "accounts"
CREDENTIAL_KEYS = {
    "GARMIN_EMAIL", "GARMIN_PASSWORD", "GARMIN_TOKEN_SALT", "COROS_EMAIL", "COROS_PASSWORD",
}
OPTION_KEYS = runtime_config.RUNTIME_ENV_KEYS | {"GARMIN_NEWEST_NUM", "SYNC_UPLOAD_WORKERS", "SYNC_DOWNLOAD_WORKERS"}
DEFAULT_OPTIONS = {
    "GARMIN_NEWEST_NUM": 0,
    "GARMIN_LOGIN_MODE": "browser",
    "GARMIN_BROWSER_TIMEOUT_SECONDS": 180,
    "SYNC_UPLOAD_WORKERS": 4,
    "SYNC_DOWNLOAD_WORKERS": 4,
}


@dataclass(frozen=True)
class Account:
    id: str
    enabled: bool
    env: dict[str, str] = field(repr=False)


def validate_id(account_id: str) -> None:
    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", account_id):
        raise ValueError("账号组 id 必须为 1–64 位字母、数字、下划线或连字符，且不能以符号开头。")


def check_fields(data: object, allowed: set[str], context: str) -> dict:
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError(f"{context} 格式错误或包含未知字段。")
    return data


def parse_options(data: object) -> dict[str, str]:
    values = check_fields(data, OPTION_KEYS, "options/defaults")
    result = {}
    for key, value in values.items():
        if key in {"SYNC_UPLOAD_WORKERS", "SYNC_DOWNLOAD_WORKERS"}:
            if type(value) is not int or not 1 <= value <= 8:
                raise ValueError(f"{key} 必须是 1–8 的整数。")
        elif key in {"GARMIN_NEWEST_NUM", "GARMIN_BROWSER_TIMEOUT_SECONDS"}:
            minimum = 0 if key == "GARMIN_NEWEST_NUM" else 1
            if type(value) is not int or value < minimum:
                raise ValueError(f"{key} 必须是合法整数。")
        elif key in {"GARMIN_BROWSER_HEADLESS", "GARMIN_BROWSER_CI_MODE"}:
            if type(value) is not bool:
                raise ValueError(f"{key} 必须是布尔值。")
            value = int(value)
        elif key == "GARMIN_LOGIN_MODE":
            if value not in ("browser", "password", ""):
                raise ValueError("GARMIN_LOGIN_MODE 不支持。")
        elif not isinstance(value, str):
            raise ValueError(f"{key} 必须是字符串。")
        result[key] = str(value)
    return result


def parse_config(data: object) -> list[Account]:
    data = check_fields(data, {"defaults", "accounts"}, "配置")
    defaults = {**parse_options(DEFAULT_OPTIONS), **parse_options(data.get("defaults", {}))}
    groups = data.get("accounts")
    if not isinstance(groups, list) or not groups:
        raise ValueError("accounts 必须是非空数组。")
    result = []
    seen = set()
    for group in groups:
        group = check_fields(group, {"id", "enabled", "direction", "garmin", "coros", "options"}, "账号组")
        account_id = group.get("id")
        validate_id(account_id)
        if account_id.casefold() in seen:
            raise ValueError("账号组 id 重复（不区分大小写）。")
        seen.add(account_id.casefold())
        enabled = group.get("enabled", True)
        if type(enabled) is not bool:
            raise ValueError(f"{account_id}: enabled 必须是布尔值。")
        # Accept the fixed direction field from existing local configurations.
        if group.get("direction", "garmin_to_coros") != "garmin_to_coros":
            raise ValueError(f"{account_id}: 仅支持 Garmin → COROS；请确认账号配置中的 direction。")
        garmin = check_fields(group.get("garmin"), {"email", "password", "token_salt", "domain"}, "garmin")
        coros = check_fields(group.get("coros"), {"email", "password"}, "coros")
        domain = garmin.get("domain", "COM")
        if domain not in ("COM", "CN"):
            raise ValueError(f"{account_id}: Garmin domain 必须是 COM 或 CN。")
        values = {"GARMIN_AUTH_DOMAIN": domain, **defaults,
                  **parse_options(group.get("options", {}))}
        for provider, fields in (("GARMIN", garmin), ("COROS", coros)):
            for name in (("email", "password", "token_salt") if provider == "GARMIN" else ("email", "password")):
                value = fields.get(name, "")
                if not isinstance(value, str) or (enabled and not value.strip()):
                    raise ValueError(f"{account_id}: {provider}_{name.upper()} 必须填写字符串。")
                values[f"{provider}_{name.upper()}"] = value
        result.append(Account(account_id, enabled, values))
    return result


def config_path(env: Mapping[str, str], path: str | Path | None = None) -> Path:
    return Path(path or env.get("SYNC_ACCOUNTS_FILE") or DEFAULT_CONFIG_FILE)


def load_accounts(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> list[Account]:
    source = os.environ if env is None else env
    target = config_path(source, path)
    inline = source.get("SYNC_ACCOUNTS_JSON") if path is None else None
    try:
        content = inline if inline is not None else target.read_text(encoding="utf-8")
        data = json.loads(content)
    except (OSError, UnicodeError, ValueError):
        raise ValueError("无法读取多账号配置或 JSON 格式错误。") from None
    return parse_config(data)


def worker_environment(
    account: Account, env: Mapping[str, str] | None = None,
    state_root: str | Path = DEFAULT_STATE_ROOT,
) -> dict[str, str]:
    source = os.environ if env is None else env
    private_keys = CREDENTIAL_KEYS | OPTION_KEYS | {
        "GARMIN_AUTH_DOMAIN", "GARMIN_ENV_FILE", "SYNC_ACCOUNTS_JSON", "SYNC_ACCOUNTS_FILE",
        "SYNC_ACCOUNT_WORKER", "SYNC_ACCOUNT_ID", "SYNC_STATE_ROOT", "SYNC_PROGRESS_TTY", "SYNC_FULL_SCAN",
    }
    proxies = {key: source[key] for key in runtime_config.RUNTIME_ENV_KEYS
               if key.lower().endswith("proxy") and key in source}
    values = {key: value for key, value in source.items()
              if key not in private_keys and key.upper() not in {"GARTH_TOKEN", "GARTH_HOME"}}
    values.update(proxies)
    values.update(account.env)
    values.update(SYNC_ACCOUNT_WORKER="1")
    values.update(SYNC_ACCOUNT_ID=account.id, SYNC_STATE_ROOT=str(Path(state_root).resolve()))
    return values


def initialize_config(path: str | Path = DEFAULT_CONFIG_FILE, seed: Account | None = None) -> Path:
    target = Path(path)
    data = json.loads(EXAMPLE_CONFIG_FILE.read_text(encoding="utf-8"))
    if seed is not None:
        account = seed
        group = data["accounts"][0]
        group["id"] = account.id
        data["accounts"] = [group, *[item for item in data["accounts"][1:]
                                    if item["id"].casefold() != account.id.casefold()]]
        for provider in ("garmin", "coros"):
            for key in group[provider]:
                env_key = "GARMIN_AUTH_DOMAIN" if key == "domain" else f"{provider.upper()}_{key.upper()}"
                group[provider][key] = account.env[env_key]
        group["options"] = {key: value for key, value in account.env.items() if key in OPTION_KEYS}
        for key in ("GARMIN_NEWEST_NUM", "GARMIN_BROWSER_TIMEOUT_SECONDS", "SYNC_UPLOAD_WORKERS", "SYNC_DOWNLOAD_WORKERS"):
            if key in group["options"]:
                group["options"][key] = int(group["options"][key])
        for key in ("GARMIN_BROWSER_HEADLESS", "GARMIN_BROWSER_CI_MODE"):
            if key in group["options"]:
                group["options"][key] = group["options"][key] == "1"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as config_file:
        json.dump(data, config_file, ensure_ascii=False, indent=2)
        config_file.write("\n")
    return target
