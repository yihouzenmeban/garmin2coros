import json
import hashlib


from coros.region_config import REGIONCONFIG
from coros.sts_config import STS_CONFIG
from utils.http_client_utils import build_http_client

class CorosClient:
    
    def __init__(self, email, password) -> None:
        
        self.email = email
        self.password = password
        self.req = build_http_client()
        self.accessToken = None
        self.userId = None
        self.regionId = None
        self.teamapi = None
    
    ## 登录接口
    def login(self):
        ## default use com login url
        login_url = "https://teamcnapi.coros.com/account/login"

        login_data = {
            "account": self.email,
            "pwd": hashlib.md5(self.password.encode()).hexdigest(), ##MD5加密密码
            "accountType":2,
        }
        headers = {
          "Accept":       "application/json, text/plain, */*",
          "Content-Type": "application/json;charset=UTF-8",
          "User-Agent":   "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.39 Safari/537.36",
          "referer": "https://teamcnapi.coros.com/",
          "origin": "https://teamcnapi.coros.com/",
        }

        login_body = json.dumps(login_data)
        response = self.req.request('POST', login_url, body=login_body, headers=headers)
        if response.status != 200:
            raise CorosLoginError(f"COROS login failed: HTTP {response.status}.")

        login_response = json.loads(response.data)
        login_result = login_response["result"]
        if login_result != "0000":
            raise CorosLoginError("COROS login rejected.")

        accessToken = login_response["data"]["accessToken"]
        userId =  login_response["data"]["userId"]
        regionId =  login_response["data"]["regionId"]
        self.accessToken = accessToken
        self.userId = userId
        self.regionId = regionId
        self.teamapi = REGIONCONFIG[self.regionId]['teamapi']

    ## 上传运动
    def uploadActivity(self, oss_object, md5, fileName, size, *, bucket: str | None = None):
        ## 判断Token 是否为空
        if self.accessToken == None:
            self.login()

        upload_url = f"{self.teamapi}/activity/fit/import"

        headers = {
          "Accept":       "application/json, text/plain, */*",
          "accesstoken": self.accessToken,
        }
     
        try:
          bucket = bucket or STS_CONFIG[self.regionId]["bucket"]
          serviceName = STS_CONFIG[self.regionId]["service"]
          data = {"source":1,"timezone":32,"bucket":f"{bucket}","md5":f"{md5}","size":size,"object":f"{oss_object}","serviceName":f"{serviceName}","oriFileName":f"{fileName}"}
          json_data = json.dumps(data)
          json_str = str(json_data)
          response = self.req.request(
              method = 'POST',
              url=upload_url,
              fields={ "jsonParameter": json_str},
              headers=headers
          )
          if not 200 <= response.status < 300:
              raise CorosActivityUploadError(
                  f"COROS activity import failed: HTTP {response.status}."
              )
          upload_response = json.loads(response.data)
          if not isinstance(upload_response, dict):
              raise CorosActivityUploadError("Invalid COROS activity import response.")
          result = upload_response.get("result")
          response_data = upload_response.get("data")
          status = response_data.get("status") if isinstance(response_data, dict) else None
          if result != "0000" or status != 2:
              raise CorosActivityUploadError(
                  f"COROS activity import rejected: result={result}, status={status}."
              )
          return True
        except CorosActivityUploadError:
            raise
        except Exception as err:
            raise CorosActivityUploadError(
                f"COROS activity import failed ({type(err).__name__})."
            ) from err


class CorosLoginError(Exception):

    def __init__(self, status):
        """Initialize."""
        super(CorosLoginError, self).__init__(status)
        self.status = status

class CorosActivityUploadError(Exception):

    def __init__(self, status):
        """Initialize."""
        super(CorosActivityUploadError, self).__init__(status)
        self.status = status
