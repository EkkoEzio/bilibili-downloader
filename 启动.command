#!/bin/bash
# bilibili download 安全启动器（macOS 双击或终端运行）
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${BD_PORT:-5050}"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || [[ ${#PORT} -gt 5 ]] \
    || ((10#$PORT < 1 || 10#$PORT > 65535)); then
  echo "错误：BD_PORT 必须是 1–65535 的整数，当前值：${PORT}"
  exit 1
fi
export BD_PORT="$PORT"

if [[ ! -f requirements.txt ]]; then
  echo "错误：未找到 requirements.txt。"
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "错误：未找到 Python 3，请先安装 Python 3.10 或更高版本。"
    exit 1
  fi
  echo "首次运行：正在创建虚拟环境……"
  python3 -m venv .venv
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
REQ_STAMP="$SCRIPT_DIR/.venv/.requirements.sha256"
REQ_HASH="$($PYTHON -c 'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("requirements.txt").read_bytes()).hexdigest())')"
OLD_HASH=""
if [[ -f "$REQ_STAMP" ]]; then
  OLD_HASH="$(<"$REQ_STAMP")"
fi
if [[ "$REQ_HASH" != "$OLD_HASH" ]]; then
  echo "依赖声明已变化，正在同步已验证版本……"
  if ! "$PYTHON" -m pip install -r requirements.txt; then
    echo "错误：依赖安装失败，请查看上方输出。"
    exit 1
  fi
  printf '%s\n' "$REQ_HASH" > "$REQ_STAMP"
fi

for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "错误：未找到 $tool。请先安装 ffmpeg，并确认 ffmpeg/ffprobe 都在 PATH 中。"
    exit 1
  fi
done

PORT_INFO="$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true)"
if [[ -n "$PORT_INFO" ]]; then
  # 同源才接管：只有确认监听进程属于本项目（工作目录为本目录且在运行
  # app.py）才向它发送信号；未知进程一律只报告，不做任何终止。
  OWN_PIDS=()
  HAS_FOREIGN=0
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    pid_cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" || true
    pid_cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ "$pid_cwd" == "$SCRIPT_DIR" && "$pid_cmd" == *app.py* ]]; then
      OWN_PIDS+=("$pid")
    else
      HAS_FOREIGN=1
    fi
  done < <(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)

  if ((HAS_FOREIGN)) || ((${#OWN_PIDS[@]} == 0)); then
    echo "错误：127.0.0.1:${PORT} 被非本项目进程占用。启动器不会终止未知进程。"
    echo "请先确认下列进程归属并正常停止它，或使用其他 BD_PORT："
    echo "$PORT_INFO"
    exit 1
  fi

  echo "检测到本项目旧实例（PID ${OWN_PIDS[*]}），正在优雅停止……"
  # 先 SIGINT（等价于在服务终端按 Ctrl-C，让 Python 正常收尾），再
  # SIGTERM；SIGKILL 只发给已再次确认工作目录归属、仍存活的进程。
  kill -INT ${OWN_PIDS[@]+"${OWN_PIDS[@]}"} 2>/dev/null || true
  waited=0
  while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
      && ((waited < 20)); do
    sleep 0.5
    waited=$((waited + 1))
  done
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    kill -TERM ${OWN_PIDS[@]+"${OWN_PIDS[@]}"} 2>/dev/null || true
    waited=0
    while lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 \
        && ((waited < 10)); do
      sleep 0.5
      waited=$((waited + 1))
    done
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    for pid in ${OWN_PIDS[@]+"${OWN_PIDS[@]}"}; do
      pid_cwd="$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" || true
      if [[ "$pid_cwd" == "$SCRIPT_DIR" ]] \
          && ps -p "$pid" >/dev/null 2>&1; then
        kill -KILL "$pid" 2>/dev/null || true
      fi
    done
    sleep 1
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误：旧实例未能停止，端口 ${PORT} 仍被占用，请手动处理后再运行。"
    exit 1
  fi
  echo "旧实例已停止，继续启动。"
fi

URL="http://127.0.0.1:${PORT}"
# 辅助进程只观察 HTTP ready 并打开浏览器；单调时钟 deadline 严格限制为
# 60 秒，网络 timeout、重试 sleep 和 open 等待都不得超过剩余时间。
"$PYTHON" - "$URL" <<'PY' &
import subprocess
import sys
import time
import urllib.error
import urllib.request

base_url = sys.argv[1]
status_url = base_url + "/api/config"
deadline = time.monotonic() + 60.0
ready = False

try:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            with urllib.request.urlopen(
                status_url,
                timeout=min(1.0, remaining),
            ) as response:
                if 200 <= response.status < 400:
                    ready = True
                    break
        except (urllib.error.URLError, TimeoutError, OSError):
            pass

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
except KeyboardInterrupt:
    raise SystemExit(0)

remaining = deadline - time.monotonic()
if ready and remaining > 0:
    print(f"服务已就绪：{base_url}")
    try:
        opened = subprocess.run(
            ["open", base_url],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=remaining,
        )
    except (OSError, subprocess.TimeoutExpired):
        opened = None
    if opened is None or opened.returncode != 0:
        print(f"提示：浏览器未自动打开，请手动访问 {base_url}")
else:
    print("提示：服务在 60 秒内未 ready；辅助进程不会停止服务。"
          "请查看终端日志并手动处理。")
PY

# 用服务替换 launcher 本身：终端信号和最终退出码直接属于 Python，
# 不保存、探测或终止任何数值进程标识，从结构上排除复用标识误伤。
exec "$PYTHON" app.py
