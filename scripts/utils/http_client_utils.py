import os

import certifi
import urllib3


def build_http_client():
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    manager_kwargs = {
        "maxsize": 8,
        "cert_reqs": "CERT_REQUIRED",
        "ca_certs": certifi.where(),
        "timeout": urllib3.Timeout(connect=10, read=30),
        "retries": urllib3.Retry(total=2, connect=2, read=2, status=2,
                                 allowed_methods=frozenset({"GET", "HEAD"}),
                                 status_forcelist=(429, 500, 502, 503, 504),
                                 backoff_factor=0.5, respect_retry_after_header=False),
    }
    if proxy_url:
        return urllib3.ProxyManager(proxy_url, **manager_kwargs)
    return urllib3.PoolManager(**manager_kwargs)
