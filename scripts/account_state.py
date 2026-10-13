from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

try:
    from .account_config import Account, DEFAULT_STATE_ROOT, validate_id
except ImportError:
    from account_config import Account, DEFAULT_STATE_ROOT, validate_id


def binding(account: Account) -> dict[str, str | int]:
    garmin = account.env["GARMIN_EMAIL"].strip().casefold() + "\n" + account.env["GARMIN_AUTH_DOMAIN"]
    coros = account.env["COROS_EMAIL"].strip().casefold()
    return {"version": 1, "id": account.id,
            "garmin": hashlib.sha256(garmin.encode()).hexdigest(),
            "coros": hashlib.sha256(coros.encode()).hexdigest()}


def same_identity(first: Account, second: Account) -> bool:
    return all(binding(first)[key] == binding(second)[key] for key in ("garmin", "coros"))


def write_binding(state: Path, account: Account) -> None:
    descriptor = os.open(state / "binding.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as metadata:
        json.dump(binding(account), metadata)
        metadata.write("\n")


def valid_binding(metadata: object, account_id: str) -> bool:
    return (
        isinstance(metadata, dict)
        and set(metadata) == {"version", "id", "garmin", "coros"}
        and type(metadata["version"]) is int and metadata["version"] == 1
        and metadata["id"] == account_id
        and all(isinstance(metadata[key], str) and re.fullmatch(r"[0-9a-f]{64}", metadata[key])
                for key in ("garmin", "coros"))
    )


def unused_database(path: Path, table: str) -> bool:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            return False
        objects = set(database.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
        ).fetchall())
        index = f"{table}_activity_id"
        if ("table", table) not in objects or objects - {("table", table), ("index", index)}:
            return False
        if ("index", index) in objects:
            if [row[2] for row in database.execute(f"PRAGMA index_info({index})")] != ["activity_id"]:
                return False
        if database.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            return False
        return database.execute("SELECT 1 FROM sqlite_sequence WHERE seq > 0 LIMIT 1").fetchone() is None


def unused_state(state: Path) -> bool:
    databases = {"garmin.db": "garmin_activity", "coros.db": "coros_activity"}
    sidecars = {name + suffix: name for name in databases for suffix in ("-wal", "-shm", "-journal")}
    try:
        for path in state.iterdir():
            if path.is_symlink():
                return False
            if path.name in {".lock", "binding.json"}:
                if not path.is_file():
                    return False
            elif path.name in databases:
                if not path.is_file() or not unused_database(path, databases[path.name]):
                    return False
            elif path.name in sidecars:
                if not path.is_file() or not (state / sidecars[path.name]).is_file():
                    return False
            elif path.name in {"garmin-fit", "coros-fit"}:
                if not path.is_dir() or any(path.iterdir()):
                    return False
            else:
                return False
        return True
    except (OSError, sqlite3.DatabaseError):
        return False


def replace_binding(state: Path, account: Account) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".binding-", suffix=".tmp", dir=state)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as metadata:
            json.dump(binding(account), metadata)
            metadata.write("\n")
            metadata.flush()
            os.fsync(metadata.fileno())
        os.replace(temporary, state / "binding.json")
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def lock_state(account: Account, root: str | Path = DEFAULT_STATE_ROOT, bind: bool = True):
    validate_id(account.id)
    root = Path(root).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = root / account.id
    if state.is_symlink():
        raise ValueError("账号状态目录不能是符号链接。")
    state.mkdir(mode=0o700, exist_ok=True)
    os.chmod(state, 0o700)
    descriptor = os.open(state / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该账号组正在运行，请稍后重试。") from None
        try:
            metadata = state / "binding.json"
            if any(path.is_symlink() for path in state.iterdir()):
                raise ValueError("账号状态文件不能是符号链接。")
            if bind:
                if metadata.exists():
                    try:
                        stored = json.loads(metadata.read_text())
                        if not valid_binding(stored, account.id):
                            raise ValueError
                    except (OSError, UnicodeError, ValueError):
                        raise ValueError("账号绑定已损坏；请检查绑定文件或使用新 id。") from None
                    if stored != binding(account):
                        if not unused_state(state):
                            raise ValueError("账号绑定不一致，状态非空或无法确认为空；请使用新 id，不能复用此组进度。")
                        replace_binding(state, account)
                elif any(path.name != ".lock" for path in state.iterdir()):
                    raise ValueError("发现未绑定的旧状态，请显式迁移或使用新 id。")
                else:
                    write_binding(state, account)
            yield state
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def migrate_legacy(
    account: Account, legacy: Account, root: str | Path = DEFAULT_STATE_ROOT,
    source_dir: str | Path | None = None,
) -> Path:
    if source_dir is None:
        source_dir = Path(__file__).resolve().parents[1] / "db"
    source = Path(source_dir)
    if not same_identity(account, legacy):
        raise ValueError("旧配置与目标账号组不匹配，拒绝迁移。")
    token = source / "garth_token.enc"
    files = [source / name for name in ("garmin.db", "coros.db", "garth_token.enc")
             if (source / name).exists()]
    if not files or any(path.is_symlink() or not path.is_file() for path in files):
        raise ValueError("没有可迁移的旧状态，或旧状态文件不安全。")
    if token.exists():
        if account.env["GARMIN_TOKEN_SALT"] != legacy.env["GARMIN_TOKEN_SALT"]:
            raise ValueError("迁移 token 时必须沿用旧 GARMIN_TOKEN_SALT。")
        try:
            try:
                from .garmin.garth_token_store import read_encrypted_token
            except ImportError:
                from garmin.garth_token_store import read_encrypted_token
            read_encrypted_token(str(token), account.env["GARMIN_TOKEN_SALT"])
        except Exception:
            raise ValueError("旧 token 无法解密，迁移未执行。") from None
    with lock_state(account, root, bind=False) as state:
        if any(path.name != ".lock" for path in state.iterdir()):
            raise ValueError("目标账号组已有状态，拒绝覆盖。")
        copied = []
        try:
            with tempfile.TemporaryDirectory(prefix=".migration-", dir=state) as directory:
                staging = Path(directory)
                for path in files:
                    target = staging / path.name
                    if path.suffix == ".db":
                        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as old_db:
                            with closing(sqlite3.connect(target)) as new_db:
                                old_db.backup(new_db)
                    else:
                        shutil.copyfile(path, target)
                    os.chmod(target, 0o600)
                write_binding(staging, account)
                for path in staging.iterdir():
                    destination = state / path.name
                    os.replace(path, destination)
                    copied.append(destination)
        except Exception:
            for path in copied:
                path.unlink()
            raise ValueError("旧状态迁移失败，目标未保留部分进度。") from None
    return state
