#!/usr/bin/env python3

import json
import os
import sys
import time
import base64
from pathlib import Path
from urllib.parse import parse_qs, urlparse

CURRENT_DIR = os.path.split(os.path.abspath(__file__))[0]
config_path = CURRENT_DIR.rsplit("/", 1)[0]
project_root = config_path.rsplit("/", 1)[0]
PROJECT_ROOT = Path(project_root)
PROJECT_VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
for path in (config_path, project_root):
    if path not in sys.path:
        sys.path.append(path)

from garmin.garth_auth import GARTH_BROWSER_USER_AGENT, GarthOAuthConsumerError
from config import dispatch_cli
from runtime_setup import prepare_runtime
from garmin.generate_garth_token import (
    apply_runtime_env,
    load_garmin_env,
)

DEFAULT_TIMEOUT_SECONDS = 300
OAUTH_CONSUMER_URL = "https://thegarth.s3.amazonaws.com/oauth_consumer.json"
OAUTH_CONSUMER_ATTEMPTS = 3
OAUTH_CONSUMER_TIMEOUT_SECONDS = 10
OAUTH_CONSUMER_RETRY_STATUSES = {429, 500, 502, 503, 504}
WEBVIEW_SSO_URL = (
    "https://sso.{domain}/sso/embed"
    "?id=gauth-widget"
    "&embedWidget=true"
    "&gauthHost=https://sso.{domain}/sso"
    "&clientId=GarminConnect"
    "&locale=en_US"
    "&redirectAfterAccountLoginUrl=https://sso.{domain}/sso/embed"
    "&service=https://sso.{domain}/sso/embed"
)
ANDROID_UA = "com.garmin.android.apps.connectmobile"
EMAIL_INPUT_SELECTORS = (
    "input[type='email']",
    "input[name*='email']",
    "input[id*='email']",
    "input[autocomplete='username']",
)
PASSWORD_INPUT_SELECTORS = (
    "input[type='password']",
    "input[name*='password']",
    "input[id*='password']",
    "input[autocomplete='current-password']",
)
SUBMIT_BUTTON_SELECTORS = (
    "button[type='submit']",
    "input[type='submit']",
)
POST_LOGIN_BUTTON_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('Accept')",
    "button:has-text('Agree')",
    "button:has-text('Remember me')",
)
MANUAL_STEP_URL_KEYWORDS = (
    "captcha",
    "challenge",
    "2fa",
    "mfa",
)
MANUAL_STEP_TEXT_KEYWORDS = (
    "verification code",
    "security code",
    "enter the code",
    "enter code",
    "two-factor",
    "two factor",
    "2fa",
    "mfa",
    "multi-factor",
    "multifactor",
    "captcha",
    "approve sign in",
    "approve this sign in",
)


def env_truthy(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def maybe_reexec_with_project_venv() -> None:
    if os.environ.get("GARMIN_SKIP_VENV_REEXEC") == "1":
        return
    if not PROJECT_VENV_PYTHON.exists():
        return

    current_python = Path(os.path.abspath(sys.executable))
    project_python = Path(os.path.abspath(str(PROJECT_VENV_PYTHON)))
    if current_python == project_python:
        return

    os.environ["GARMIN_SKIP_VENV_REEXEC"] = "1"
    os.execv(
        str(project_python),
        [str(project_python), os.path.abspath(__file__), *sys.argv[1:]],
    )


def resolve_runtime() -> dict[str, object]:
    try:
        from requests import get
        from requests_oauthlib import OAuth1Session
        from garmin.garth_token_store import write_encrypted_token
        from config import GARTH_TOKEN_FILE
    except ModuleNotFoundError as exc:
        if exc.name not in {"config", "garmin"}:
            raise RuntimeError(
                "缺少 Python 依赖 "
                f"`{exc.name}`。请先确认已安装 requirements.txt 中依赖"
            ) from exc
        try:
            from requests import get
            from requests_oauthlib import OAuth1Session
            from scripts.garmin.garth_token_store import write_encrypted_token
            from scripts.config import GARTH_TOKEN_FILE
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                "缺少 Python 依赖 "
                f"`{exc.name}`。请先确认已安装 requirements.txt 中依赖"
            ) from exc
    runtime = {}
    runtime["http_get"] = get
    runtime["oauth1_session_class"] = OAuth1Session
    runtime["write_encrypted_token"] = write_encrypted_token
    runtime["token_path"] = GARTH_TOKEN_FILE
    return runtime


def resolve_domain(auth_domain: str) -> str:
    if auth_domain and str(auth_domain).upper() == "CN":
        return "garmin.cn"
    return "garmin.com"


def extract_service_ticket_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    ticket = parse_qs(parsed.query).get("ticket", [None])[0]
    return str(ticket) if ticket else None


def build_playwright_launch_options() -> dict[str, object]:
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )
    options: dict[str, object] = {
        "headless": env_truthy("GARMIN_BROWSER_HEADLESS", default=env_truthy("CI")),
    }
    if proxy_url:
        options["proxy"] = {"server": proxy_url}
    return options


def _load_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "缺少 Playwright 依赖。请先运行:\n"
            "1. .venv/bin/pip install playwright\n"
            "2. .venv/bin/python -m playwright install chromium"
        ) from exc
    return sync_playwright


def _find_first_visible_locator(page, selectors):
    if not hasattr(page, "locator"):
        return None
    for selector in selectors:
        locator = page.locator(selector)
        try:
            if locator.count() and locator.first.is_visible():
                return locator.first
        except Exception:
            continue
    return None


def remaining_ms(deadline: float, maximum: int | None = None) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise TimeoutError("Garmin 浏览器登录超过设定时间。")
    return min(remaining, maximum) if maximum is not None else remaining


def _fill_locator(locator, value: str, deadline: float | None = None) -> bool:
    if locator is None:
        return False
    try:
        current_value = locator.input_value(**({"timeout": remaining_ms(deadline, 3000)} if deadline else {}))
    except Exception:
        current_value = ""
    if current_value != value:
        locator.fill(value, **({"timeout": remaining_ms(deadline, 3000)} if deadline else {}))
    return True


def _click_first_visible_locator(page, selectors, deadline: float | None = None) -> bool:
    locator = _find_first_visible_locator(page, selectors)
    if locator is None:
        return False
    locator.click(timeout=remaining_ms(deadline, 3000) if deadline else 3000)
    return True


def requires_manual_takeover(page) -> bool:
    current_url = str(getattr(page, "url", "")).lower()
    if any(keyword in current_url for keyword in MANUAL_STEP_URL_KEYWORDS):
        return True

    if hasattr(page, "locator"):
        try:
            body = page.locator("body")
            content = ""
            if body.count():
                content = str(body.first.inner_text()).lower()
        except Exception:
            content = ""
    else:
        content = ""

    if any(keyword in content for keyword in MANUAL_STEP_TEXT_KEYWORDS):
        return True

    return False


def attempt_auto_login(page, login_email: str | None, login_password: str | None,
                       deadline: float | None = None) -> bool:
    if not login_email or not login_password:
        return False

    email_locator = _find_first_visible_locator(page, EMAIL_INPUT_SELECTORS)
    password_locator = _find_first_visible_locator(page, PASSWORD_INPUT_SELECTORS)
    submit_locator = _find_first_visible_locator(page, SUBMIT_BUTTON_SELECTORS)

    if not email_locator or not password_locator or not submit_locator:
        return False

    _fill_locator(email_locator, login_email, deadline)
    _fill_locator(password_locator, login_password, deadline)
    submit_locator.click(timeout=remaining_ms(deadline, 3000) if deadline else 3000)
    return True


def capture_service_ticket(
    domain: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    playwright_factory=None,
    login_email: str | None = None,
    login_password: str | None = None,
) -> str:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

    deadline = time.monotonic() + max(int(timeout_seconds), 1)
    playwright_factory = playwright_factory or _load_sync_playwright()
    sign_in_url = WEBVIEW_SSO_URL.format(domain=domain)
    ci_mode = env_truthy("GARMIN_BROWSER_CI_MODE", default=env_truthy("GITHUB_ACTIONS"))

    with playwright_factory() as playwright:
        browser = playwright.chromium.launch(timeout=remaining_ms(deadline, 30000), **build_playwright_launch_options())
        try:
            context = browser.new_context(user_agent=GARTH_BROWSER_USER_AGENT)
            page = context.new_page()
            page.goto(sign_in_url, wait_until="domcontentloaded", timeout=remaining_ms(deadline))
            page.set_default_timeout(remaining_ms(deadline, 3000))
            has_login_credentials = bool(login_email and login_password)
            auto_login_attempted = attempt_auto_login(page, login_email, login_password, deadline=deadline)

            if ci_mode:
                print("浏览器已打开，当前为 CI/headless 模式，将自动尝试 Garmin 登录。")
            else:
                print("浏览器已打开，请在页面中完成 Garmin 登录和 MFA。")
            if auto_login_attempted:
                print("脚本已自动填写 Garmin 账号密码，并尝试提交登录表单。")
            elif has_login_credentials:
                print("脚本会继续等待登录表单渲染，并自动尝试填写 Garmin 账号密码。")
            else:
                print("脚本暂未完成自动填表，请在浏览器中继续登录。")
            print("登录成功后，脚本会自动抓取 serviceTicketId 并生成加密 token。")

            ticket = None
            manual_takeover_announced = False
            while time.monotonic() < deadline and not ticket:
                page.set_default_timeout(remaining_ms(deadline, 3000))
                ticket = extract_service_ticket_from_url(page.url)
                if ticket or page.is_closed():
                    break
                if not auto_login_attempted and has_login_credentials:
                    auto_login_attempted = attempt_auto_login(page, login_email, login_password, deadline=deadline)
                    if auto_login_attempted:
                        print("检测到 Garmin 登录表单已就绪，脚本已自动填写并提交。")
                _click_first_visible_locator(page, POST_LOGIN_BUTTON_SELECTORS, deadline=deadline)
                ticket = extract_service_ticket_from_url(page.url)
                if ticket:
                    break
                if not manual_takeover_announced and requires_manual_takeover(page):
                    if ci_mode:
                        raise RuntimeError("CI 模式下 Garmin 登录进入 MFA/验证码/确认页，无法人工接管。")
                    manual_takeover_announced = True
                    print("检测到 Garmin 需要人工接管（MFA/验证码/确认页），请在浏览器中完成后等待脚本继续。")
                page.wait_for_timeout(remaining_ms(deadline, 500))
        except PlaywrightTimeoutError:
            raise TimeoutError("Garmin 浏览器登录操作超时。") from None
        finally:
            browser.close()

    if ticket:
        return ticket
    raise TimeoutError(
        f"在 {timeout_seconds} 秒内未捕获到 Garmin serviceTicketId。"
    )


def validate_oauth_consumer(consumer: object) -> dict[str, str]:
    if not isinstance(consumer, dict) or not all(
        isinstance(consumer.get(key), str) and consumer[key].strip()
        for key in ("consumer_key", "consumer_secret")
    ):
        raise GarthOAuthConsumerError("响应缺少有效的 OAuth 参数")
    return {
        "consumer_key": consumer["consumer_key"],
        "consumer_secret": consumer["consumer_secret"],
    }


def _get_oauth_consumer_via_browser(playwright_factory=None) -> dict[str, str]:
    factory = playwright_factory or _load_sync_playwright()
    try:
        with factory() as playwright:
            options = build_playwright_launch_options()
            options["headless"] = True
            browser = playwright.chromium.launch(**options)
            try:
                page = browser.new_page()
                response = page.goto(
                    OAUTH_CONSUMER_URL,
                    wait_until="domcontentloaded",
                    timeout=OAUTH_CONSUMER_TIMEOUT_SECONDS * 1000,
                )
                if response is None:
                    raise GarthOAuthConsumerError("浏览器未收到 HTTP 响应")
                if not response.ok:
                    raise GarthOAuthConsumerError(f"浏览器 HTTP {response.status}")
                try:
                    consumer = response.json()
                except ValueError:
                    raise GarthOAuthConsumerError("浏览器响应不是有效的 JSON") from None
                return validate_oauth_consumer(consumer)
            finally:
                browser.close()
    except GarthOAuthConsumerError:
        raise
    except Exception:
        raise GarthOAuthConsumerError("TLS/网络连接异常（Requests 和 Chromium 均失败）") from None


def get_oauth_consumer(http_get, playwright_factory=None) -> dict[str, str]:
    from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

    for attempt in range(OAUTH_CONSUMER_ATTEMPTS):
        try:
            response = http_get(OAUTH_CONSUMER_URL, timeout=OAUTH_CONSUMER_TIMEOUT_SECONDS)
            try:
                response.raise_for_status()
                return validate_oauth_consumer(response.json())
            finally:
                response.close()
        except (ConnectionError, Timeout):
            failure = "TLS/网络连接异常"
        except HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            failure = f"HTTP {status}" if status is not None else "HTTP 请求失败"
            if status not in OAUTH_CONSUMER_RETRY_STATUSES:
                raise GarthOAuthConsumerError(failure) from None
        except ValueError:
            raise GarthOAuthConsumerError("响应不是有效的 JSON") from None
        except RequestException:
            raise GarthOAuthConsumerError("HTTPS 请求失败") from None
        if attempt + 1 < OAUTH_CONSUMER_ATTEMPTS:
            print(
                f"Garmin OAuth 参数下载失败（{failure}），"
                f"重试 {attempt + 1}/{OAUTH_CONSUMER_ATTEMPTS - 1}",
                flush=True,
            )
            time.sleep(attempt + 1)

    print("Requests 下载 Garmin OAuth 参数失败，尝试通过无头 Chromium 访问同一地址。", flush=True)
    return _get_oauth_consumer_via_browser(playwright_factory=playwright_factory)


def get_oauth1_token(
    ticket: str,
    domain: str,
    consumer: dict[str, str],
    oauth1_session_class,
) -> dict[str, str]:
    session = oauth1_session_class(
        consumer["consumer_key"],
        consumer["consumer_secret"],
    )
    url = (
        f"https://connectapi.{domain}/oauth-service/oauth/"
        f"preauthorized?ticket={ticket}"
        f"&login-url=https://sso.{domain}/sso/embed"
        "&accepts-mfa-tokens=true"
    )
    response = session.get(
        url,
        headers={"User-Agent": ANDROID_UA},
        timeout=15,
    )
    response.raise_for_status()
    token = {key: values[0] for key, values in parse_qs(response.text).items()}
    token["domain"] = domain
    return token


def exchange_oauth2_token(
    oauth1_token: dict[str, str],
    consumer: dict[str, str],
    oauth1_session_class,
) -> dict[str, object]:
    session = oauth1_session_class(
        consumer["consumer_key"],
        consumer["consumer_secret"],
        resource_owner_key=oauth1_token["oauth_token"],
        resource_owner_secret=oauth1_token["oauth_token_secret"],
    )
    data = {}
    if oauth1_token.get("mfa_token"):
        data["mfa_token"] = oauth1_token["mfa_token"]
    response = session.post(
        f"https://connectapi.{oauth1_token['domain']}/oauth-service/oauth/exchange/user/2.0",
        headers={
            "User-Agent": ANDROID_UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=data,
        timeout=15,
    )
    response.raise_for_status()
    token = response.json()
    token["expires_at"] = int(time.time() + token["expires_in"])
    token["refresh_token_expires_at"] = int(
        time.time() + token["refresh_token_expires_in"]
    )
    return token


def encode_garth_token(oauth1_token: dict[str, object], oauth2_token: dict[str, object]) -> str:
    payload = json.dumps([oauth1_token, oauth2_token])
    return base64.b64encode(payload.encode("utf-8")).decode("utf-8")


def fetch_username(http_get, domain: str, access_token: str) -> str | None:
    response = http_get(
        f"https://connectapi.{domain}/userprofile-service/socialProfile",
        headers={
            "User-Agent": GARTH_BROWSER_USER_AGENT,
            "Authorization": f"Bearer {access_token}",
        },
        timeout=15,
    )
    response.raise_for_status()
    profile = response.json()
    if isinstance(profile, dict):
        return profile.get("userName")
    return None


def generate_token_via_browser(
    runtime: dict[str, object] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    playwright_factory=None,
) -> dict[str, object]:
    apply_runtime_env(env=env)
    config = load_garmin_env(env=env)
    if runtime is None and playwright_factory is None:
        prepare_runtime(browser_required=True)
    runtime = runtime or resolve_runtime()

    token_path = Path(str(runtime["token_path"]))
    write_encrypted_token = runtime["write_encrypted_token"]
    http_get = runtime["http_get"]
    oauth1_session_class = runtime["oauth1_session_class"]

    existed_before = token_path.exists()
    mtime_before = token_path.stat().st_mtime if existed_before else None

    garmin_domain = resolve_domain(config["GARMIN_AUTH_DOMAIN"])
    consumer = get_oauth_consumer(http_get, playwright_factory=playwright_factory)
    ticket = capture_service_ticket(
        domain=garmin_domain,
        timeout_seconds=timeout_seconds,
        playwright_factory=playwright_factory,
        login_email=config["GARMIN_EMAIL"],
        login_password=config["GARMIN_PASSWORD"],
    )
    oauth1_token = get_oauth1_token(
        ticket=ticket,
        domain=garmin_domain,
        consumer=consumer,
        oauth1_session_class=oauth1_session_class,
    )
    oauth2_token = exchange_oauth2_token(
        oauth1_token=oauth1_token,
        consumer=consumer,
        oauth1_session_class=oauth1_session_class,
    )
    garth_token = encode_garth_token(oauth1_token, oauth2_token)
    username = None
    try:
        username = fetch_username(
            http_get=http_get,
            domain=garmin_domain,
            access_token=str(oauth2_token["access_token"]),
        )
    except Exception:
        pass
    write_encrypted_token(
        str(token_path),
        config["GARMIN_TOKEN_SALT"],
        garth_token,
    )

    token_exists = token_path.exists()
    mtime_after = token_path.stat().st_mtime if token_exists else None
    updated = token_exists and (not existed_before or mtime_after != mtime_before)

    return {
        "token_path": str(token_path),
        "token_exists": token_exists,
        "token_updated": updated,
        "token_existed_before": existed_before,
        "garmin_domain": garmin_domain,
        "garmin_email": config["GARMIN_EMAIL"],
        "username": username,
        "login_mode": "browser",
    }


def main() -> int:
    try:
        maybe_reexec_with_project_venv()
        timeout_seconds = int(
            os.environ.get("GARMIN_BROWSER_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
        )
        result = generate_token_via_browser(timeout_seconds=timeout_seconds)
    except Exception as exc:
        print(f"Generate Garmin token with browser failed: {exc}", file=sys.stderr)
        return 1

    print("Generate Garmin token with browser succeeded.")
    print(f"Domain: {result['garmin_domain']}")
    print(f"Email: {result['garmin_email']}")
    print(f"Username: {result['username']}")
    print(f"Token path: {result['token_path']}")
    print(f"Token existed before: {result['token_existed_before']}")
    print(f"Token exists now: {result['token_exists']}")
    print(f"Token updated this run: {result['token_updated']}")
    return 0


if __name__ == "__main__":
    dispatch_cli(refresh_token="browser")
    raise SystemExit(main())
