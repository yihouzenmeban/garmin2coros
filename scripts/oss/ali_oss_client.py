import oss2
import os
import threading

from oss2 import SizedFileAdapter, determine_part_size
from oss2.models import PartInfo
from oss.coros_sts_client import fetch_sts_credentials
from utils.http_client_utils import build_http_client


class AliOssClient:
    def __init__(
        self,
        bucket="coros-oss",
        service="aliyun",
        v=2,
        *,
        access_token: str | None = None,
        region_id: int = 2,
    ):
        self.bucket = bucket
        self.service = service
        self.access_token = access_token
        self.region_id = region_id
        self.security_token = None
        self.access_key_id = None
        self.access_key_secret = None
        self.req = build_http_client()
        self.client = None
        self._upload_clients = threading.local()
        self.v = v
        self.initClient()

    def initClient(self):
        credients_json = fetch_sts_credentials(
            self.req,
            bucket=self.bucket,
            service=self.service,
            access_token=self.access_token,
            region_id=self.region_id,
            version=self.v,
        )
        self.bucket = credients_json["Bucket"]
        region = credients_json["Region"]
        if not region.startswith("oss-"):
            region = f"oss-{region}"
        SecurityToken = credients_json["SecurityToken"]
        AccessKeyId = credients_json["AccessKeyId"]
        AccessKeySecret = credients_json["AccessKeySecret"]
        self.security_token = SecurityToken
        self.access_key_id = AccessKeyId
        self.access_key_secret = AccessKeySecret

        auth = oss2.StsAuth(self.access_key_id, self.access_key_secret, self.security_token)
        self.client = oss2.Bucket(auth, f"https://{region}.aliyuncs.com", self.bucket, connect_timeout=30)
        self._upload_clients.client = self.client

    def _upload_client(self):
        if not hasattr(self._upload_clients, "client"):
            self._upload_clients.client = oss2.Bucket(
                self.client.auth, self.client.endpoint, self.bucket, connect_timeout=30,
            )
        return self._upload_clients.client
    
    def multipart_upload(self, filePath, fileName):
        client = self._upload_client()
        key = f"fit_zip/{fileName}"
        total_size = os.path.getsize(filePath)
        if total_size < 5 * 1024 * 1024:
            result = client.put_object_from_file(key, filePath)
            if result.status != 200:
                raise AliOssError("阿里云文件上传失败")
            return key
        init_multipart_upload_result = client.init_multipart_upload(key)
        if init_multipart_upload_result.status != 200:
            raise AliOssError("初始化阿里云分片上传异常")
        upload_id = init_multipart_upload_result.upload_id
        # determine_part_size方法用于确定分片大小。
        part_size = determine_part_size(total_size, preferred_size=1024 * 1024)
        parts = []

        # 逐个上传分片。
        try:
            with open(filePath, 'rb') as fileobj:
                part_number = 1
                offset = 0
                while offset < total_size:
                    num_to_upload = min(part_size, total_size - offset)
                    result = client.upload_part(key, upload_id, part_number,
                                                SizedFileAdapter(fileobj, num_to_upload))
                    if result.status != 200:
                        raise AliOssError("阿里云分片上传失败")
                    parts.append(PartInfo(part_number, result.etag))
                    offset += num_to_upload
                    part_number += 1
            result = client.complete_multipart_upload(key, upload_id, parts)
            if result.status != 200:
                raise AliOssError("阿里云分片合并失败")
        except Exception:
            try:
                client.abort_multipart_upload(key, upload_id)
            except Exception:
                pass
            raise
        return key
    



class AliOssError(RuntimeError):
    def __init__(self, status):
      """Initialize."""
      super(AliOssError, self).__init__(status)
      self.status = status
