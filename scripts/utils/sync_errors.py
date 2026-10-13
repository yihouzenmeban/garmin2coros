import ssl
from urllib.parse import urlsplit


GARMIN_AUTH_EXIT_CODE = 3
TLS_REASONS = (
    "CERTIFICATE_VERIFY_FAILED", "UNEXPECTED_EOF_WHILE_READING",
    "WRONG_VERSION_NUMBER", "SSLV3_ALERT_HANDSHAKE_FAILURE",
    "TLSV1_ALERT_INTERNAL_ERROR", "TLSV1_ALERT_PROTOCOL_VERSION",
)
NETWORK_HOSTS = {
    "connectapi.garmin.com", "connectapi.garmin.cn",
    "sso.garmin.com", "sso.garmin.cn", "thegarth.s3.amazonaws.com",
    "diauth.garmin.com", "diauth.garmin.cn",
}


class GarminAuthenticationError(RuntimeError):
    pass


def response_status(error: Exception) -> int | None:
    response = getattr(error, "response", None)
    if response is None:
        response = getattr(getattr(error, "error", None), "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_garmin_auth_error(error: Exception) -> bool:
    return isinstance(error, GarminAuthenticationError) or response_status(error) in {401, 403}


def error_chain(error: Exception) -> list[BaseException]:
    pending: list[BaseException] = [error]
    result = []
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        result.append(current)
        nested = (*current.args, current.__cause__, current.__context__,
                  getattr(current, "reason", None), getattr(current, "error", None))
        pending.extend(value for value in nested if isinstance(value, BaseException))
    return result


def tls_reasons(error: Exception) -> set[str]:
    reasons = set()
    for current in error_chain(error):
        # Only emit known reason codes; exception text can contain credentials and URLs.
        reasons.update(reason for reason in TLS_REASONS if reason in str(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            reasons.add("CERTIFICATE_VERIFY_FAILED")
        if isinstance(current, ssl.SSLEOFError):
            reasons.add("UNEXPECTED_EOF_WHILE_READING")
    return reasons


def is_transient_network_error(error: Exception) -> bool:
    from requests.exceptions import ConnectionError, SSLError, Timeout

    if not isinstance(error, (ConnectionError, Timeout)):
        return False
    reasons = tls_reasons(error)
    if reasons or any(isinstance(current, (SSLError, ssl.SSLError)) for current in error_chain(error)):
        return reasons == {"UNEXPECTED_EOF_WHILE_READING"}
    return True


def network_error_detail(error: Exception) -> str:
    detail = [type(error).__name__, *sorted(tls_reasons(error))]
    hosts = set()
    for current in error_chain(error):
        request = getattr(current, "request", None)
        url = getattr(request, "url", None)
        if isinstance(url, str):
            try:
                host = urlsplit(url).hostname
            except ValueError:
                continue
            if host in NETWORK_HOSTS:
                hosts.add(host)
    detail.extend(f"目标域名 {host}" for host in sorted(hosts))
    return "；".join(detail)
