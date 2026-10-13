#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
PYTHON_BIN="${PYTHON_BIN:-}"

log() {
  printf '[sync] %s\n' "$*"
}

usage() {
  cat <<'EOF'
用法:
  ./run_sync.sh
  ./run_sync.sh --headless-only
  ./run_sync.sh --browser-only

说明:
  默认先尝试无头同步；仅 Garmin 认证失败时自动弹出浏览器刷新 token 后再重试同步。
  --headless-only  只跑无头同步，不自动弹浏览器
  --browser-only   先弹浏览器刷新 token，再执行同步
EOF
}

pick_python() {
  if [[ -n "$PYTHON_BIN" ]]; then
    return
  fi

  if [[ -x "$VENV_PYTHON" ]]; then
    PYTHON_BIN="$VENV_PYTHON"
  else
    PYTHON_BIN="$(command -v python3 || true)"
  fi

  if [[ -z "$PYTHON_BIN" ]]; then
    log "未找到可用的 Python 解释器。"
    exit 1
  fi
}

validate_env() {
  local missing=()
  local key

  for key in GARMIN_AUTH_DOMAIN GARMIN_EMAIL GARMIN_PASSWORD GARMIN_TOKEN_SALT COROS_EMAIL COROS_PASSWORD; do
    if [[ -z "${!key:-}" ]]; then
      missing+=("$key")
    fi
  done

  if (( ${#missing[@]} > 0 )); then
    log "缺少必要环境变量: ${missing[*]}"
    log "请在 config.local.json 的当前账号组中填写完整凭据"
    exit 1
  fi
}

run_headless_sync() {
  local sync_script="$ROOT_DIR/scripts/garmin/garmin_sync_coros.py"
  log "开始无头同步"
  GARMIN_BROWSER_HEADLESS=1 \
  GARMIN_BROWSER_CI_MODE=1 \
  "$PYTHON_BIN" "$sync_script"
}

refresh_token_in_browser() {
  log "打开浏览器刷新 Garmin token"
  GARMIN_BROWSER_HEADLESS=0 \
  GARMIN_BROWSER_CI_MODE=0 \
  GARMIN_BROWSER_TIMEOUT_SECONDS="${GARMIN_BROWSER_TIMEOUT_SECONDS:-600}" \
  "$PYTHON_BIN" "$ROOT_DIR/scripts/garmin/generate_garth_token_browser.py"
}

main() {
  if [[ "${SYNC_ACCOUNT_WORKER:-}" != "1" ]]; then
    pick_python
    exec "$PYTHON_BIN" "$ROOT_DIR/scripts/sync_accounts.py" "$@"
  fi
  local mode="${1:-default}"

  case "$mode" in
    --help|-h)
      usage
      exit 0
      ;;
    --headless-only|--browser-only|default)
      ;;
    *)
      log "不支持的参数: $mode"
      usage
      exit 1
      ;;
  esac

  pick_python
  validate_env

  if [[ "$mode" == "--browser-only" ]]; then
    refresh_token_in_browser
    run_headless_sync
    return
  fi

  if [[ "$mode" == "--headless-only" ]]; then
    run_headless_sync
    return
  fi

  local sync_status=0
  if run_headless_sync; then
    log "无头同步完成"
    return
  else
    sync_status=$?
  fi

  if [[ "$sync_status" != "3" ]]; then
    log "同步失败，已保留失败记录供下次重试"
    return "$sync_status"
  fi
  log "Garmin 认证失败，切换到可见浏览器刷新 token 后重试"
  refresh_token_in_browser
  run_headless_sync
  log "浏览器回退同步完成"
}

main "$@"
