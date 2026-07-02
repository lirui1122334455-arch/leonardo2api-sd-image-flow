#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PID_FILE="${PID_FILE:-$APP_DIR/fpbrowser2api.pid}"
LOG_FILE="${LOG_FILE:-$APP_DIR/fpbrowser2api.out}"
LOGS_DIR="${LOGS_DIR:-$APP_DIR/logs}"

# fpbrowser2api 项目根目录常见的调试/文件日志
DEBUG_LOG_FILE="${DEBUG_LOG_FILE:-$APP_DIR/logs.txt}"
APP_LOG_FILE="${APP_LOG_FILE:-$APP_DIR/app.log}"

# 优先使用项目内虚拟环境，其次使用系统 python
PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    PYTHON_BIN="$APP_DIR/.venv/bin/python"
  elif [[ -x "$APP_DIR/venv/bin/python" ]]; then
    PYTHON_BIN="$APP_DIR/venv/bin/python"
  elif [[ -x "$APP_DIR/.venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$APP_DIR/.venv/Scripts/python.exe"
  elif [[ -x "$APP_DIR/venv/Scripts/python.exe" ]]; then
    PYTHON_BIN="$APP_DIR/venv/Scripts/python.exe"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    PYTHON_BIN="python"
  fi
fi

resolve_app_launch() {
  # 打包发布模式：优先运行 APP_BIN 或同目录下的 fpbrowser2api 可执行文件
  if [[ -n "${APP_BIN:-}" ]]; then
    if [[ ! -x "$APP_BIN" ]]; then
      echo "APP_BIN 不存在或不可执行: $APP_BIN" >&2
      return 1
    fi
    APP_CMD=("$APP_BIN")
    APP_CMD_DISPLAY="$APP_BIN"
    return 0
  fi

  if [[ -x "$APP_DIR/fpbrowser2api" ]]; then
    APP_CMD=("$APP_DIR/fpbrowser2api")
    APP_CMD_DISPLAY="$APP_DIR/fpbrowser2api"
    return 0
  fi

  # 源码开发模式：回退到 python main.py
  if [[ -f "$APP_DIR/main.py" ]]; then
    APP_CMD=("$PYTHON_BIN" "$APP_DIR/main.py")
    APP_CMD_DISPLAY="$PYTHON_BIN $APP_DIR/main.py"
    return 0
  fi

  echo "未找到可运行入口：$APP_DIR/fpbrowser2api 或 $APP_DIR/main.py；打包发布目录应包含 fpbrowser2api" >&2
  return 1
}

is_running() {
  [[ -f "$PID_FILE" ]] || return 1
  local pid
  pid="$(cat "$PID_FILE")"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

rotate_and_truncate() {
  # 轮转单个文件到 logs/ 目录，并把原文件置空
  # - 若文件不存在：创建空文件
  # - 若文件为空：仅置空（保持存在）
  local src="$1"
  local prefix="$2"

  mkdir -p "$LOGS_DIR"

  if [[ -f "$src" ]] && [[ -s "$src" ]]; then
    local ts dest
    ts="$(date +"%Y%m%d_%H%M%S")"
    dest="$LOGS_DIR/${prefix}_${ts}_$RANDOM.txt"
    mv "$src" "$dest"
    echo "已备份旧日志: $src -> $dest"
  fi

  : > "$src"
}

start() {
  if is_running; then
    echo "fpbrowser2api 已在运行 (pid=$(cat "$PID_FILE"))"
    return 0
  fi

  cd "$APP_DIR"

  # 启动前：备份并清空调试日志（logs.txt）
  rotate_and_truncate "$DEBUG_LOG_FILE" "logs"

  # 启动前：备份并清空 app.log（当 log_to_file=true 才会写入）
  if [[ -f "$APP_LOG_FILE" ]]; then
    rotate_and_truncate "$APP_LOG_FILE" "app"
  fi

  # 启动前：清空服务输出日志（fpbrowser2api.out）
  : > "$LOG_FILE"

  resolve_app_launch

  # 后台运行 + 输出到日志文件：
  # - 打包发布目录：./fpbrowser2api
  # - 源码开发目录：python main.py
  nohup "${APP_CMD[@]}" >>"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  sleep 1
  if is_running; then
    echo "fpbrowser2api 启动成功 (pid=$(cat "$PID_FILE")), cmd=$APP_CMD_DISPLAY, log=$LOG_FILE"
    return 0
  fi

  echo "fpbrowser2api 启动失败，请查看日志: $LOG_FILE" >&2
  return 1
}

stop() {
  if ! [[ -f "$PID_FILE" ]]; then
    echo "fpbrowser2api 未运行（找不到 pid 文件: $PID_FILE）"
    return 0
  fi

  local pid
  pid="$(cat "$PID_FILE" || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$PID_FILE"
    echo "pid 文件为空，已清理: $PID_FILE"
    return 0
  fi

  if ! kill -0 "$pid" >/dev/null 2>&1; then
    rm -f "$PID_FILE"
    echo "fpbrowser2api 进程不存在，已清理 pid 文件: $PID_FILE"
    return 0
  fi

  echo "正在停止 fpbrowser2api (pid=$pid)..."
  kill "$pid" >/dev/null 2>&1 || true

  # 最多等待 30 秒优雅退出
  for _ in $(seq 1 30); do
    if kill -0 "$pid" >/dev/null 2>&1; then
      sleep 1
    else
      break
    fi
  done

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "优雅停止超时，强制杀进程 (pid=$pid)"
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi

  rm -f "$PID_FILE"
  echo "fpbrowser2api 已停止"
}

status() {
  if is_running; then
    echo "fpbrowser2api 运行中 (pid=$(cat "$PID_FILE"))"
  else
    echo "fpbrowser2api 未运行"
  fi
}

restart() {
  stop
  start
}

usage() {
  cat <<'EOF'
用法:
  ./fpbrowser2api_service.sh start|stop|restart|status

可选环境变量:
  APP_BIN=/path/to/fpbrowser2api
  PYTHON_BIN=/path/to/python
  PID_FILE=/path/to/fpbrowser2api.pid
  LOG_FILE=/path/to/fpbrowser2api.out
  DEBUG_LOG_FILE=/path/to/logs.txt
  APP_LOG_FILE=/path/to/app.log
  LOGS_DIR=/path/to/logs_dir
EOF
}

cmd="${1:-}"
case "$cmd" in
  start) start ;;
  stop) stop ;;
  restart) restart ;;
  status) status ;;
  *) usage; exit 1 ;;
esac

