#!/bin/bash
# bilibili download 启动脚本
# 双击此文件即可启动,浏览器打开 http://localhost:5050
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "首次运行,正在创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip -q 2>&1
  # 安装依赖:失败时显示完整错误(不用 -q,让用户看到具体原因)
  if ! pip install -r requirements.txt 2>&1; then
    echo ""
    echo "============================================"
    echo "依赖安装失败!请看上面的错误信息。"
    echo "常见原因:网络问题、Python 版本不兼容。"
    echo "============================================"
    read -n 1 -s -r -p "按任意键关闭窗口..."
    exit 1
  fi
else
  source .venv/bin/activate
fi

# 先关闭上次的实例:否则旧进程占着端口,新代码起不来、页面还是旧的
# (端口与 config.py 的 PORT 保持一致)
OLD_PID=$(lsof -ti tcp:5050 2>/dev/null)
if [ -n "$OLD_PID" ]; then
  echo "检测到正在运行的实例(PID $OLD_PID),正在关闭..."
  kill $OLD_PID 2>/dev/null
  for i in 1 2 3 4 5; do
    lsof -ti tcp:5050 >/dev/null 2>&1 || break
    sleep 1
  done
  if lsof -ti tcp:5050 >/dev/null 2>&1; then
    echo "实例未正常退出,强制结束。"
    kill -9 $(lsof -ti tcp:5050) 2>/dev/null
    sleep 1
  fi
fi

# 自动在浏览器打开:等端口就绪再开(冷启动装依赖时 2 秒可能还没起好)
(
  for i in $(seq 1 60); do
    if curl -s -o /dev/null "http://localhost:5050/api/config"; then
      open "http://localhost:5050"
      exit 0
    fi
    sleep 1
  done
  echo "提示:端口 5050 60 秒内未就绪,请手动打开浏览器访问。"
) &

python3 app.py
read -n 1 -s -r -p "按任意键关闭窗口..."
