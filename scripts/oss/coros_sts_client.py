from http.cookies import SimpleCookie
import json
from typing import Any

import urllib3

from coros.region_config import REGIONCONFIG
from oss.sts_token_error import StsTokenError
from utils.coros_oss_credients_utils import decode


def fetch_sts_credentials(
    http_client,
    *,
    bucket: str,
    service: str,
    access_token: str | None,
    region_id: int,
    version: int = 2,
) -> dict[str, Any]:
    if not isinstance(access_token, str) or not access_token:
        raise StsTokenError("COROS login is required to obtain upload credentials.")
    region_config = REGIONCONFIG.get(region_id)
    if region_config is None:
        raise StsTokenError("Unsupported COROS region for upload credentials.")
    service_fields = {
        "aliyun": ("AccessKeySecret", "SecurityToken"),
        "aws": ("SecretAccessKey", "SessionToken"),
    }
    if service not in service_fields:
        raise StsTokenError("Unsupported COROS storage service.")

    cookies = SimpleCookie()
    cookies[region_config["TOKEN"]] = access_token
    cookies[region_config["CREGION"]] = str(region_id)
    cookie_header = cookies.output(header="", sep="; ").strip()
    url = f"{region_config['hostname']}/api/proxy/oss/sts"
    try:
        response = http_client.request(
            "GET",
            url,
            fields={"bucket": bucket, "service": service, "v": version},
            headers={"Cookie": cookie_header, "Accept": "application/json"},
            timeout=urllib3.Timeout(connect=10, read=30),
        )
    except Exception as error:
        raise StsTokenError(
            f"COROS STS request failed ({type(error).__name__})."
        ) from error

    if response.status != 200:
        raise StsTokenError(f"COROS STS request failed: HTTP {response.status}.")
    try:
        payload = json.loads(response.data)
    except (ValueError, TypeError) as error:
        raise StsTokenError("COROS STS response is not valid JSON (HTTP 200).") from error

    code = payload.get("code") if isinstance(payload, dict) else None
    if code != 200:
        safe_code = code if isinstance(code, int) else "unknown"
        raise StsTokenError(f"COROS STS request rejected: code={safe_code}.")
    data = payload.get("data")
    encoded_credentials = data.get("credentials") if isinstance(data, dict) else None
    if not isinstance(encoded_credentials, str) or not encoded_credentials:
        raise StsTokenError("COROS STS response has no credentials.")
    try:
        credentials = decode(encoded_credentials)
    except Exception as error:
        raise StsTokenError(
            f"COROS STS credentials could not be decoded ({type(error).__name__})."
        ) from error

    required_fields = ("AccessKeyId", "Region", "Bucket", *service_fields[service])
    if not isinstance(credentials, dict):
        raise StsTokenError("COROS STS credentials are not a JSON object.")
    missing = [
        field for field in required_fields
        if not isinstance(credentials.get(field), str) or not credentials[field]
    ]
    if missing:
        raise StsTokenError(
            f"COROS STS credentials are missing required fields: {', '.join(missing)}."
        )
    return credentials
