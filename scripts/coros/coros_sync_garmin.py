import time

MODULE_STARTED = time.monotonic()

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(CURRENT_DIR))

from config import dispatch_cli

if __name__ == "__main__":
    dispatch_cli(direction="coros_to_garmin")

from coros.coros_client import CorosClient
from config import DB_DIR, COROS_FIT_DIR, load_sync_config
from coros.coros_db import CorosDB
from utils.sync_progress import FailureSummary, SyncProgress
from utils.activity_scan import ScanState
from utils.sync_timing import StageTimings
from utils.sync_errors import GARMIN_AUTH_EXIT_CODE, is_garmin_auth_error, network_error_detail

MODULE_LOAD_SECONDS = time.monotonic() - MODULE_STARTED


SYNC_CONFIG = {
    "GARMIN_AUTH_DOMAIN": "",
    "GARMIN_EMAIL": "",
    "GARMIN_PASSWORD": "",
    "GARMIN_TOKEN_SALT": "",
    "GARMIN_NEWEST_NUM": 0,
    "COROS_EMAIL": "",
    "COROS_PASSWORD": "",
}


def init(coros_db):
    os.makedirs(DB_DIR, exist_ok=True)
    if not os.path.exists(os.path.join(DB_DIR, coros_db.coros_db_name)):
        coros_db.initDB()
    os.makedirs(COROS_FIT_DIR, exist_ok=True)


def main() -> int:
    print(f"[sync] 模块加载耗时：{MODULE_LOAD_SECONDS:.2f}s", flush=True)
    timings = StageTimings()
    garmin_client = None
    stage = "读取配置"
    try:
        sync_config = load_sync_config(SYNC_CONFIG)
        coros_client = CorosClient(sync_config["COROS_EMAIL"], sync_config["COROS_PASSWORD"])
        stage = "初始化状态"
        database = CorosDB("coros.db")
        init(database)
        stage = "登录 COROS"
        with timings.measure(stage):
            coros_client.login()
        stage = "获取 COROS 运动"
        known_ids = database.getKnownActivityIds()
        scan = ScanState(os.path.join(DB_DIR, "coros-scan.json"))
        full_scan = scan.should_full_scan(known_ids, requested=os.environ.get("SYNC_FULL_SCAN") == "1")
        print(f"扫描模式：{'完整扫描' if full_scan else '增量扫描'}", flush=True)
        with timings.measure(stage):
            activities = coros_client.getAllActivities(known_ids=known_ids, full_scan=full_scan)
        if not isinstance(activities, list):
            raise ValueError("Invalid activity response")
        print(f"获取 COROS 运动：{len(activities)} 条")
        with timings.measure("读取同步状态"):
            database.saveActivities(activities)
            if full_scan:
                scan.mark_complete()
        pending = database.getUnSyncActivity() or []
        print(f"待同步：{len(pending)} 条（包含此前失败记录）")
        progress = SyncProgress(len(pending))
        failures = FailureSummary()
        needs_auth = False
        if pending:
            stage = "Garmin 登录"
            with timings.measure(stage):
                from garmin.garmin_client import GarminClient

                garmin_client = GarminClient(
                    sync_config["GARMIN_EMAIL"], sync_config["GARMIN_PASSWORD"],
                    sync_config["GARMIN_AUTH_DOMAIN"], sync_config["GARMIN_NEWEST_NUM"],
                    token_salt=sync_config["GARMIN_TOKEN_SALT"],
                )
                garmin_client.ensure_login()
        for activity in pending:
            activity_id = activity["id"]
            stage = "下载"
            try:
                with timings.measure(stage):
                    downloaded = coros_client.downloadActivitie(activity_id, activity["sportType"])
                file_path = os.path.join(COROS_FIT_DIR, f"{activity_id}.fit")
                with open(file_path, "wb") as activity_file:
                    activity_file.write(downloaded.data)
                stage = "上传"
                with timings.measure(stage):
                    status = garmin_client.upload_activity(file_path)
                if status not in ("SUCCESS", "DUPLICATE_ACTIVITY"):
                    raise RuntimeError("Garmin activity import rejected")
                database.updateSyncStatus(activity_id)
            except Exception as error:
                if stage == "上传" and is_garmin_auth_error(error):
                    needs_auth = True
                database.updateExceptionSyncStatus(activity_id)
                failures.add(stage, type(error).__name__)
                progress.advance(False)
            else:
                progress.advance(True)
        failures.write()
        print(f"同步结果：成功 {progress.succeeded} 条，失败 {progress.failed} 条")
        return GARMIN_AUTH_EXIT_CODE if needs_auth else 1 if progress.failed else 0
    except Exception as error:
        print(f"{stage}失败：{network_error_detail(error)}", file=sys.stderr)
        return GARMIN_AUTH_EXIT_CODE if stage == "Garmin 登录" and is_garmin_auth_error(error) else 1
    finally:
        if garmin_client is not None:
            garmin_client.close()
        timings.write()


if __name__ == "__main__":
    raise SystemExit(main())
