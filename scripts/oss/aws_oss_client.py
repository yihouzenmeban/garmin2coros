import boto3

from boto3.s3.transfer import TransferConfig
from botocore.config import Config


from oss.coros_sts_client import fetch_sts_credentials
from utils.http_client_utils import build_http_client

class AwsOssClient:
  def __init__(
      self,
      bucket="eu-coros",
      service="aws",
      v=2,
      *,
      access_token: str | None = None,
      region_id: int = 3,
  ):
    self.bucket = bucket
    self.service = service
    self.access_token = access_token
    self.region_id = region_id
    self.credentials = None
    self.access_key_id = None
    self.access_key_secret = None
    self.req = build_http_client()
    self.v = v
    self.client = None
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
        self.credentials = credients_json
        self.bucket = credients_json["Bucket"]
        self.client = boto3.client(
            "s3",
            region_name=credients_json["Region"],
            aws_access_key_id=credients_json["AccessKeyId"],
            aws_secret_access_key=credients_json["SecretAccessKey"],
            aws_session_token=credients_json["SessionToken"],
            config=Config(connect_timeout=10, read_timeout=30, max_pool_connections=8,
                          retries={"max_attempts": 2, "mode": "standard"}),
        )

  def multipart_upload(self, filePath, fileName):
      # 配置上传选项
      config = TransferConfig(
          multipart_threshold=1024 * 1024 * 5,  # 分片上传的阈值（5MB）
          max_concurrency=1,
          multipart_chunksize=1024 * 1024 * 5,  # 分片大小（5MB）
          use_threads=False
      )

      # 执行上传
      object_key = f"fit_zip/{fileName}"
      self.client.upload_file(
          filePath,
          Bucket=self.bucket,
          Key=object_key,
          Config=config
      )
      return object_key
