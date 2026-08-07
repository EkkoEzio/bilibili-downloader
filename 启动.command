#!/bin/bash
# bilibili download 启动脚本
# 双击此文件即可启动,浏览器打开 http://localhost:5050
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "首次运行,正在创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip -q
  pip install -r requirements.txt -q
else
  source .venv/bin/activate
fi

# 自动在浏览器打开(后台执行,2秒后)
( sleep 2 && open "http://localhost:5050" ) &

python3 app.py
read -n 1 -s -r -p "按任意键关闭窗口..."
