#!/bin/bash
# 双击启动器：调用同目录的 hexun_scraper.py
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

PY=""
for cand in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ]; then
  echo "未找到 python3。请先安装：在终端执行 xcode-select --install"
  read -n 1 -s -r -p "按任意键关闭..."
  exit 1
fi

"$PY" "$DIR/hexun_scraper.py"
EXIT=$?
if [ $EXIT -ne 0 ]; then
  echo ""
  echo "脚本以非零退出码结束：$EXIT"
  read -n 1 -s -r -p "按任意键关闭..."
fi
