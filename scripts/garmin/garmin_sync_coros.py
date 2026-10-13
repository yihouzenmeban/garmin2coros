import time

MODULE_STARTED = time.monotonic()

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
import os
import sys
import threading

CURRENT_DIR = os.path.split(os.path.abspath(__file__))[0]  # 当前目录
config_path = CURRENT_DIR.rsplit('/', 1)[0]  # 上三级目录
sys.path.append(config_path)

from config import dispatch_cli

if __name__ == "__main__":
    dispatch_cli(direction="garmin_to_coros")

from config import DB_DIR, GARMIN_FIT_DIR, load_sync_config
from garmin.garmin_client import GarminClient
from garmin.garmin_db import GarminDB
from garmin.garth_auth import GarthOAuthConsumerError
from coros.coros_client import CorosActivityUploadError, CorosClient
from coros.sts_config import STS_CONFIG
from oss.sts_token_error import StsTokenError
from utils.md5_utils import calculate_md5_file
from utils.sync_progress import FailureSummary, SyncProgress
from utils.activity_scan import ScanState
from utils.sync_timing import StageTimings
from utils.sync_errors import GARMIN_AUTH_EXIT_CODE, is_garmin_auth_error, network_error_detail

MODULE_LOAD_SECONDS = time.monotonic() - MODULE_STARTED

SYNC_CONFIG = {
    'GARMIN_AUTH_DOMAIN': '',
    'GARMIN_EMAIL': '',
    'GARMIN_PASSWORD': '',
    'GARMIN_TOKEN_SALT': '',
    'GARMIN_NEWEST_NUM': 0,
    "COROS_EMAIL": '',
    "COROS_PASSWORD": '',
    "SYNC_UPLOAD_WORKERS": 4,
    "SYNC_DOWNLOAD_WORKERS": 4,
}


def init(garmin_db):
    os.makedirs(DB_DIR, exist_ok=True)
    if not os.path.exists(os.path.join(DB_DIR, garmin_db.garmin_db_name)):
        garmin_db.initDB()
    os.makedirs(GARMIN_FIT_DIR, exist_ok=True)


def error_detail(error: Exception) -> str:
    if isinstance(error, (CorosActivityUploadError, StsTokenError, GarthOAuthConsumerError)):
        return str(error)
    return network_error_detail(error)


def report_failure(stage: str, error: Exception) -> None:
    print(f"{stage}失败：{error_detail(error)}", file=sys.stderr, flush=True)


def create_storage_client(coros_client):
    storage_config = STS_CONFIG[coros_client.regionId]
    if coros_client.regionId == 2:
        from oss.ali_oss_client import AliOssClient

        client_class = AliOssClient
    elif coros_client.regionId in (1, 3):
        from oss.aws_oss_client import AwsOssClient

        client_class = AwsOssClient
    else:
        raise ValueError("Unsupported COROS region.")
    return client_class(
        bucket=storage_config["bucket"], service=storage_config["service"],
        access_token=coros_client.accessToken, region_id=coros_client.regionId,
    )


def upload_activity(activity_id, file_path, coros_client, storage_client, timings=None):
    stage = "上传"
    try:
        file_md5 = calculate_md5_file(file_path)
        with timings.measure(stage) if timings else nullcontext():
            object_key = storage_client.multipart_upload(
                file_path, f"{coros_client.userId}/{file_md5}.zip",
            )
        stage = "导入 COROS"
        with timings.measure(stage) if timings else nullcontext():
            accepted = coros_client.uploadActivity(
                object_key, file_md5, f"{activity_id}.zip", os.path.getsize(file_path),
                bucket=storage_client.bucket,
            )
        if not accepted:
            raise CorosActivityUploadError("COROS activity import was not accepted.")
    except Exception as error:
        return stage, error
    return stage, None


def main() -> int:
    print(f"[sync] 模块加载耗时：{MODULE_LOAD_SECONDS:.2f}s", flush=True)
    timings = StageTimings()
    garmin_client = None
    stage = "初始化同步"
    try:
        sync_config = load_sync_config(SYNC_CONFIG)
        garmin_db = GarminDB("garmin.db")
        init(garmin_db)
        garmin_client = GarminClient(
            sync_config["GARMIN_EMAIL"],
            sync_config["GARMIN_PASSWORD"],
            sync_config["GARMIN_AUTH_DOMAIN"],
            sync_config["GARMIN_NEWEST_NUM"],
            token_salt=sync_config["GARMIN_TOKEN_SALT"],
        )

        stage = "Garmin 登录"
        with timings.measure(stage):
            garmin_client.ensure_login()
        stage = "获取 Garmin 运动"
        known_ids = garmin_db.getKnownActivityIds()
        scan = ScanState(os.path.join(DB_DIR, "garmin-scan.json"))
        requested = os.environ.get("SYNC_FULL_SCAN") == "1"
        full_scan = requested or (not int(sync_config["GARMIN_NEWEST_NUM"]) and scan.should_full_scan(known_ids))
        scan_mode = "完整扫描" if full_scan else "增量扫描"
        if not full_scan and int(sync_config["GARMIN_NEWEST_NUM"]):
            scan_mode = f"限量增量扫描（最多 {int(sync_config['GARMIN_NEWEST_NUM'])} 条）"
        print(f"扫描模式：{scan_mode}", flush=True)
        with timings.measure(stage):
            activities = garmin_client.getAllActivities(known_ids=known_ids, full_scan=full_scan)
        if not isinstance(activities, list):
            raise ValueError("Invalid Garmin activity list.")
        print(f"获取 Garmin 运动：{len(activities)} 条", flush=True)

        stage = "读取同步状态"
        with timings.measure(stage):
            garmin_db.saveActivities([activity["activityId"] for activity in activities])
            if full_scan:
                scan.mark_complete()
            pending_ids = list(dict.fromkeys(garmin_db.getUnSyncActivity() or []))
        print(f"待同步：{len(pending_ids)} 条（包含此前失败记录）", flush=True)
        progress = SyncProgress(len(pending_ids))
        failures = FailureSummary()
        if not pending_ids:
            print("同步结果：成功 0 条，失败 0 条", flush=True)
            return 0

        stage = "登录 COROS"
        coros_client = CorosClient(
            sync_config["COROS_EMAIL"], sync_config["COROS_PASSWORD"]
        )
        with timings.measure(stage):
            coros_client.login()

        max_workers = int(sync_config["SYNC_UPLOAD_WORKERS"])
        download_slots = threading.BoundedSemaphore(int(sync_config["SYNC_DOWNLOAD_WORKERS"]))
        futures = {}
        needs_auth = False

        def finish_activity(activity_id, failed_stage, error):
            nonlocal stage, needs_auth
            if failed_stage == "下载" and error is not None and is_garmin_auth_error(error):
                needs_auth = True
            if error is None:
                stage = "更新同步状态"
                try:
                    garmin_db.updateSyncStatus(activity_id)
                except Exception as update_error:
                    failed_stage, error = stage, update_error
            if error is not None:
                stage = "记录同步失败"
                garmin_db.updateExceptionSyncStatus(activity_id)
                failures.add(failed_stage, error_detail(error))
            progress.advance(error is None)

        def drain_uploads(block=False):
            if not futures:
                return
            done, _ = wait(futures, timeout=None if block else 0, return_when=FIRST_COMPLETED)
            for future in done:
                activity_id = futures.pop(future)
                failed_stage, error = future.result()
                finish_activity(activity_id, failed_stage, error)

        stage = "准备上传"
        try:
            with timings.measure(stage):
                storage_client = create_storage_client(coros_client)
        except Exception as error:
            for activity_id in pending_ids:
                finish_activity(activity_id, "准备上传", error)
            failures.write()
            print(f"同步结果：成功 0 条，失败 {progress.failed} 条", flush=True)
            return 1

        stage = "准备下载会话"
        with timings.measure(stage):
            garmin_client.prepare_downloads()

        def transfer(activity_id):
            try:
                with download_slots, timings.measure("下载"):
                    archive = garmin_client.downloadFitActivity(activity_id)
                    file_path = os.path.join(GARMIN_FIT_DIR, f"{activity_id}.zip")
                    with open(file_path, "wb") as archive_file:
                        archive_file.write(archive)
            except Exception as error:
                return "下载", error
            return upload_activity(activity_id, file_path, coros_client, storage_client, timings)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for activity_id in pending_ids:
                if len(futures) >= max_workers:
                    drain_uploads(block=True)
                futures[pool.submit(transfer, activity_id)] = activity_id
                drain_uploads()
            while futures:
                drain_uploads(block=True)

        failures.write()
        print(
            f"同步结果：成功 {progress.succeeded} 条，失败 {progress.failed} 条",
            flush=True,
        )
        return GARMIN_AUTH_EXIT_CODE if needs_auth else 1 if progress.failed else 0
    except Exception as error:
        report_failure(stage, error)
        return GARMIN_AUTH_EXIT_CODE if stage in {"获取 Garmin 运动", "Garmin 登录", "准备下载会话"} and is_garmin_auth_error(error) else 1
    finally:
        if garmin_client is not None:
            garmin_client.close_downloads()
            garmin_client.upload_session.close()
        timings.write()


if __name__ == "__main__":
    raise SystemExit(main())
