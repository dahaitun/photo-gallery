#!/bin/bash
# 启动私人相册服务（macOS）
# 用法：./start.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 检查 Python
if ! command -v python3 &>/dev/null; then
  echo "❌ 未找到 python3，请先安装 Python 3.9+"
  exit 1
fi

# 创建虚拟环境（首次）
if [ ! -d ".venv" ]; then
  echo "📦 首次运行：创建虚拟环境并安装依赖..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo "✅ 依赖安装完成"
fi

# 读取端口配置
PORT=$(python3 -c "import yaml; c=yaml.safe_load(open('config.yaml')); print(c.get('port',8080))" 2>/dev/null || echo "8080")

echo ""
echo "📸 私人相册服务启动中..."
echo "   本机访问：http://localhost:$PORT"

# 获取局域网 IP
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "未知")
if [ "$LAN_IP" != "未知" ]; then
  echo "   局域网访问：http://$LAN_IP:$PORT"
fi
echo ""
echo "   按 Ctrl+C 停止服务"
echo ""

# 启动服务
.venv/bin/python server.py
