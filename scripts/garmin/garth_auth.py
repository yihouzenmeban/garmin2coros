import os


GARTH_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class GarthOAuthConsumerError(RuntimeError):
    def __init__(self, detail: str):
        super().__init__(
            f"Garmin OAuth 参数下载失败：{detail}；"
            "请检查 thegarth.s3.amazonaws.com 的 HTTPS 连通性，"
            "以及 config.local.json 的 HTTPS_PROXY/NO_PROXY 设置。"
        )


def load_garth():
    os.environ["GARTH_TELEMETRY_ENABLED"] = "false"
    os.environ["GARTH_TELEMETRY_SEND_TO_LOGFIRE"] = "false"
    import garth

    garth.configure(telemetry_enabled=False, telemetry_send_to_logfire=False, timeout=30, retries=2)
    return garth


def configure_domain(garth_module, auth_domain: str) -> str:
    if auth_domain and str(auth_domain).upper() == "CN":
        domain = "garmin.cn"
    else:
        domain = "garmin.com"
    garth_module.configure(domain=domain)
    return domain


def apply_browser_user_agent(garth_module) -> None:
    garth_module.client.sess.headers["User-Agent"] = GARTH_BROWSER_USER_AGENT


def remove_garth_user_agent(garth_module) -> None:
    garth_module.client.sess.headers.pop("User-Agent", None)
