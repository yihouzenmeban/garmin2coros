from collections.abc import Mapping
import os
from pathlib import Path
import re
import shlex
import tempfile

try:
    from .account_config import (
        Account, OPTION_KEYS, initialize_config, load_accounts, parse_config, validate_id,
    )
    from .account_state import lock_state, migrate_legacy, same_identity
except ImportError:
    from account_config import (
        Account, OPTION_KEYS, initialize_config, load_accounts, parse_config, validate_id,
    )
    from account_state import lock_state, migrate_legacy, same_identity


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEGACY_FILE = PROJECT_ROOT / ".env.garmin.local"


def read_legacy_values(path: str | Path) -> dict[str, str]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        raise ValueError("无法读取待迁移的旧配置。") from None
    values = {}
    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        try:
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError
            parts = shlex.split(raw_value, comments=True)
            if len(parts) > 1:
                raise ValueError
        except ValueError:
            raise ValueError(f"旧配置第 {line_number} 行格式错误。") from None
        values[key] = parts[0] if parts else ""
    return values


def legacy_profile(values: Mapping[str, str], account_id: str) -> Account:
    options = {}
    for key in OPTION_KEYS:
        if key not in values:
            continue
        value = values[key]
        if key in {"GARMIN_NEWEST_NUM", "GARMIN_BROWSER_TIMEOUT_SECONDS", "SYNC_UPLOAD_WORKERS", "SYNC_DOWNLOAD_WORKERS"}:
            if not value:
                continue
            try:
                value = int(value)
            except ValueError:
                raise ValueError(f"{key} 必须是合法整数。") from None
        elif key in {"GARMIN_BROWSER_HEADLESS", "GARMIN_BROWSER_CI_MODE"}:
            if value.lower() not in ("true", "false", "1", "0"):
                raise ValueError(f"{key} 必须是布尔值。")
            value = value.lower() in ("true", "1")
        options[key] = value
    return parse_config({"accounts": [{
        "id": account_id, "options": options,
        "garmin": {"email": values.get("GARMIN_EMAIL", ""),
                   "password": values.get("GARMIN_PASSWORD", ""),
                   "token_salt": values.get("GARMIN_TOKEN_SALT", ""),
                   "domain": values.get("GARMIN_AUTH_DOMAIN") or "COM"},
        "coros": {"email": values.get("COROS_EMAIL", ""),
                  "password": values.get("COROS_PASSWORD", "")},
    }]})[0]


def migrate_configuration(
    env_file: str | Path, config_file: str | Path, account_id: str,
    state_root: str | Path, source_dir: str | Path,
    backup_root: str | Path | None = None,
) -> Path:
    validate_id(account_id)
    source_env = Path(env_file)
    target = Path(config_file)
    if source_env.is_symlink() or target.is_symlink():
        raise ValueError("迁移配置文件不能是符号链接。")
    legacy = legacy_profile(read_legacy_values(source_env), account_id)
    created = False
    if target.exists():
        matching = [account for account in load_accounts(target, env={})
                    if account.id == account_id and account.enabled]
        if not matching or not same_identity(matching[0], legacy):
            raise ValueError("现有 JSON 配置与旧账号不匹配，拒绝覆盖。")
        account = matching[0]
    else:
        initialize_config(target, seed=legacy)
        created = True
        account = legacy
    try:
        source = Path(source_dir)
        if any((source / name).exists() for name in ("garmin.db", "garth_token.enc")):
            migrate_legacy(account, legacy, state_root, source)
        else:
            with lock_state(account, state_root):
                pass
    except Exception:
        if created:
            target.unlink()
        raise
    root = Path(backup_root) if backup_root is not None else PROJECT_ROOT.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup_directory = Path(tempfile.mkdtemp(prefix=".garmin-sync-coros-config-backup-", dir=root))
    backup = backup_directory / source_env.name
    os.chmod(source_env, 0o600)
    os.replace(source_env, backup)
    return backup
