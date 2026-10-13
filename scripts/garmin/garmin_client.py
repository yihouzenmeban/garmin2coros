import logging
import os
import threading
import time
from functools import wraps

import requests

try:
    from config import GARTH_TOKEN_FILE
    from utils.sync_errors import (
        GarminAuthenticationError, is_garmin_auth_error, response_status,
        is_transient_network_error, network_error_detail,
    )
    from garmin.garth_auth import (
        GarthOAuthConsumerError,
        apply_browser_user_agent,
        configure_domain,
        load_garth,
        remove_garth_user_agent,
    )
    from garmin.garth_token_store import (
        GarthTokenStoreError,
        has_encrypted_token,
        read_encrypted_token,
        write_encrypted_token,
    )
except ModuleNotFoundError:
    from scripts.config import GARTH_TOKEN_FILE
    from scripts.utils.sync_errors import (
        GarminAuthenticationError, is_garmin_auth_error, response_status,
        is_transient_network_error, network_error_detail,
    )
    from scripts.garmin.garth_auth import (
        GarthOAuthConsumerError,
        apply_browser_user_agent,
        configure_domain,
        load_garth,
        remove_garth_user_agent,
    )
    from scripts.garmin.garth_token_store import (
        GarthTokenStoreError,
        has_encrypted_token,
        read_encrypted_token,
        write_encrypted_token,
    )

GARMIN_URL_DICT = {
    "garmin_connect_activities": "/activitylist-service/activities/search/activities",
    "garmin_connect_fit_download": "/download-service/files/activity",
}

garth = load_garth()

logger = logging.getLogger(__name__)


class GarminClient:
  def __init__(
      self,
      email,
      password,
      auth_domain,
      newest_num=0,
      token_salt=None,
      token_path=GARTH_TOKEN_FILE,
  ):
        self.auth_domain = auth_domain
        self.email = email
        self.password = password
        self.garthClient = garth
        self.newestNum = int(newest_num)
        self.token_salt = token_salt
        self.token_path = token_path
        self._download_snapshot = None
        self._download_local = threading.local()
        self._download_clients = []
        self._download_lock = threading.Lock()

  def _configure_domain(self):
      configure_domain(self.garthClient, self.auth_domain)

  def _remove_garth_user_agent(self):
      remove_garth_user_agent(self.garthClient)

  def _apply_browser_user_agent(self):
      apply_browser_user_agent(self.garthClient)

  def _persist_garth_token(self):
      if not self.token_salt:
          logger.warning(
              "GARMIN_TOKEN_SALT is not configured, skip GARTH_TOKEN persistence."
          )
          return
      garth_token = self.garthClient.client.dumps()
      write_encrypted_token(self.token_path, self.token_salt, garth_token)

  def _restore_session_from_token(self):
      if not self.token_salt:
          return False
      if not has_encrypted_token(self.token_path):
          return False

      garth_token = read_encrypted_token(self.token_path, self.token_salt)
      self.garthClient.client.loads(garth_token)
      # Touch a protected resource to confirm the restored token is still usable.
      self.garthClient.client.username
      self._remove_garth_user_agent()
      self._persist_garth_token()
      return True

  def _login_with_password(self):
      if not self.email or not self.password:
          raise GarminNoLoginException(
              "GARMIN_EMAIL and GARMIN_PASSWORD are required when GARTH_TOKEN cannot be restored."
          )
      self._configure_domain()
      self._apply_browser_user_agent()
      self.garthClient.login(self.email, self.password)
      self._remove_garth_user_agent()
      self._persist_garth_token()

  def _should_use_browser_login(self):
      return str(os.environ.get("GARMIN_LOGIN_MODE", "")).strip().lower() == "browser"

  def _login_with_browser(self):
      if not self.token_salt:
          raise GarminNoLoginException(
              "GARMIN_TOKEN_SALT is required when GARMIN_LOGIN_MODE=browser."
          )
      try:
          from .generate_garth_token_browser import generate_token_via_browser

          generate_token_via_browser(
              env={
                  "GARMIN_AUTH_DOMAIN": self.auth_domain,
                  "GARMIN_EMAIL": self.email,
                  "GARMIN_PASSWORD": self.password,
                  "GARMIN_TOKEN_SALT": self.token_salt,
              },
              timeout_seconds=int(os.environ.get("GARMIN_BROWSER_TIMEOUT_SECONDS", "300")),
          )
      except GarminAuthenticationError:
          raise
      except GarthOAuthConsumerError:
          raise
      except RuntimeError:
          raise GarminNoLoginException("Garmin 浏览器认证失败。") from None
      except TimeoutError:
          raise GarminNoLoginException("Garmin 浏览器认证超时。") from None
      if not self._restore_session_from_token():
          raise GarminNoLoginException(
              "GARTH_TOKEN browser refresh completed, but session restore still failed."
          )

  def ensure_login(self):
      delays = (60, 180)
      for attempt in range(len(delays) + 1):
          try:
              self._ensure_login_once()
              return
          except Exception as error:
              if not is_transient_network_error(error) or attempt == len(delays):
                  raise
              logger.warning(
                  "Garmin 登录网络异常：%s；%s 秒后重试（%s/%s）",
                  network_error_detail(error), delays[attempt], attempt + 2, len(delays) + 1,
              )
              time.sleep(delays[attempt])

  def _ensure_login_once(self):
      try:
          self.garthClient.client.username
          self._remove_garth_user_agent()
          return
      except Exception as error:
          if (isinstance(error, requests.exceptions.RequestException) or response_status(error) is not None) and not is_garmin_auth_error(error):
              raise
          logger.debug("No active Garmin session; trying saved GARTH_TOKEN.")

      try:
          if self._restore_session_from_token():
              return
      except GarthTokenStoreError as err:
          logger.warning("Failed to restore GARTH_TOKEN: %s", type(err).__name__)
      except Exception as err:
          if (isinstance(err, requests.exceptions.RequestException) or response_status(err) is not None) and not is_garmin_auth_error(err):
              raise
          logger.warning("GARTH_TOKEN validation failed: %s", type(err).__name__)

      if self._should_use_browser_login():
          self._login_with_browser()
          return

      self._login_with_password()

  ## 登录装饰器
  def login(func):
    @wraps(func)
    def ware(self, *args, **kwargs):
      try:
          self.ensure_login()
          return func(self, *args, **kwargs)
      except GarminAuthenticationError:
          raise
      except Exception as error:
          if is_garmin_auth_error(error):
              raise GarminAuthenticationError("Garmin 认证失效。") from None
          raise
    return ware
  
  @login 
  def download(self, path, **kwargs):
     return self.garthClient.download(path, **kwargs)
  
  @login 
  def connectapi(self, path, **kwargs):
      return self.garthClient.connectapi(path, **kwargs)
     

  ## 获取运动
  def getActivities(self, start:int, limit:int):
     
     params = {"start": str(start), "limit": str(limit)}
     activities =  self.connectapi(path=GARMIN_URL_DICT["garmin_connect_activities"], params=params)
     return activities;

  ## 获取所有运动
  def getAllActivities(self, known_ids: set[int] | None = None, full_scan: bool = False):
    all_activities = []
    start = 0
    known_pages = 0
    maximum = 0 if full_scan else self.newestNum
    while not maximum or start < maximum:
      limit = min(100, maximum - start) if maximum else 100
      activities = self.getActivities(start=start, limit=limit)
      if not isinstance(activities, list) or any(not isinstance(item, dict) or "activityId" not in item for item in activities):
          raise ValueError("Invalid Garmin activity list.")
      all_activities.extend(activities)
      known_pages = known_pages + 1 if known_ids and all(item["activityId"] in known_ids for item in activities) else 0
      if len(activities) < limit or (not full_scan and known_pages >= 2):
          break
      start += limit
    return all_activities

  ## 下载原始格式的运动
  def prepare_downloads(self):
    self.ensure_login()
    self._download_snapshot = self.garthClient.client.dumps()

  def close_downloads(self):
    for client in self._download_clients:
        client.sess.close()
    self._download_clients.clear()
    self._download_snapshot = None
    self._download_local = threading.local()

  def downloadFitActivity(self, activity):
    download_fit_activity_url_prefix = GARMIN_URL_DICT["garmin_connect_fit_download"]
    download_fit_activity_url = f"{download_fit_activity_url_prefix}/{activity}"
    if self._download_snapshot is None:
        return self.download(download_fit_activity_url)
    if not hasattr(self._download_local, "client"):
        client = self.garthClient.Client()
        client.loads(self._download_snapshot)
        client.configure(domain=self.garthClient.client.domain, timeout=30, retries=2,
                         telemetry_enabled=False, telemetry_send_to_logfire=False)
        client.sess.headers.pop("User-Agent", None)
        self._download_local.client = client
        with self._download_lock:
            self._download_clients.append(client)
    response = self._download_local.client.download(download_fit_activity_url)
    return response


class GarminNoLoginException(GarminAuthenticationError):
    """Raised when rate limit is exceeded."""

    def __init__(self, status):
        """Initialize."""
        super(GarminNoLoginException, self).__init__(status)
        self.status = status
