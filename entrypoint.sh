#!/bin/sh
# 📸 私人相册 — 容器启动脚本
# 支持环境变量配置相册库，无需手动编辑任何文件

CONFIG_FILE="/app/config.yaml"
DOCKER_CONFIG="/app/config.docker.yaml"
LIBRARY_FILE="/app/data/library.json"

echo "╔══════════════════════════════════════╗"
echo "║   📸 私人相册服务 v2.0 启动中...     ║"
echo "╚══════════════════════════════════════╝"

# ── 1. 使用 Docker 专用配置 ──────────────────────────────────
if [ -f "$DOCKER_CONFIG" ]; then
    echo "🐳 检测到 Docker 环境，使用 config.docker.yaml"
    cp "$DOCKER_CONFIG" "$CONFIG_FILE"
fi

# ── 2. 确保 data 目录存在 ────────────────────────────────────
mkdir -p /app/data/cache

# ── 3. 生成 library.json ─────────────────────────────────────
# 优先级：环境变量 LIBRARIES > 已有 library.json > 默认
#
# 环境变量格式（JSON 数组）：
#   LIBRARIES='[{"name":"我的相册","path":"/photos"},{"name":"归档","path":"/archive"}]'
#
# 如果没有设置 LIBRARIES，但设置了 PHOTO_ROOT（兼容旧版）：
#   PHOTO_ROOT=/photos

GENERATE_LIB=false

if [ -n "$LIBRARIES" ]; then
    echo "📋 从环境变量 LIBRARIES 生成 library.json..."
    # 验证 JSON 格式
    if echo "$LIBRARIES" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
        # 给每个库加上 enabled: true（如果没有的话）
        echo "$LIBRARIES" | python3 -c "
import json, sys
libs = json.load(sys.stdin)
for lib in libs:
    lib.setdefault('enabled', True)
json.dump(libs, sys.stdout, ensure_ascii=False, indent=2)
" > "$LIBRARY_FILE"
        echo "✅ library.json 已生成"
        GENERATE_LIB=true
    else
        echo "⚠️ LIBRARIES 格式无效，使用默认配置"
    fi
fi

if [ "$GENERATE_LIB" = false ] && [ -n "$PHOTO_ROOT" ]; then
    echo "📋 从环境变量 PHOTO_ROOT 生成 library.json..."
    LIB_NAME="${PHOTO_ROOT_NAME:-默认相册}"
    cat > "$LIBRARY_FILE" << EOF
[{"name": "$LIB_NAME", "path": "$PHOTO_ROOT", "enabled": true}]
EOF
    echo "✅ library.json 已生成（路径: $PHOTO_ROOT）"
    GENERATE_LIB=true
fi

if [ "$GENERATE_LIB" = false ] && [ ! -f "$LIBRARY_FILE" ]; then
    echo "📋 未配置相册库，使用默认路径 /photos"
    echo '[{"name": "默认相册", "path": "/photos", "enabled": true}]' > "$LIBRARY_FILE"
    echo "✅ 已创建默认 library.json"
    echo "💡 提示：在 docker-compose.yml 中设置 LIBRARIES 环境变量来配置相册库"
fi

# ── 4. 修正已有 library.json 中的宿主机路径 ──────────────────
if [ -f "$LIBRARY_FILE" ] && grep -q "/Users/\|/home/\|C:\\\\\|D:\\\\" "$LIBRARY_FILE" 2>/dev/null; then
    echo "🔧 检测到宿主机路径，尝试修正..."
    # 移除明显不在容器内的路径
    python3 -c "
import json
with open('$LIBRARY_FILE', 'r') as f:
    libs = json.load(f)
changed = False
for lib in libs:
    p = lib.get('path', '')
    if p.startswith('/Users/') or p.startswith('/home/') or ':\\\\' in p:
        print(f'  ⚠️ 跳过无效路径: {p} ({lib.get(\"name\", \"\")})')
        lib['enabled'] = False
        changed = True
if changed:
    with open('$LIBRARY_FILE', 'w') as f:
        json.dump(libs, f, ensure_ascii=False, indent=2)
    print('  ✅ 已禁用无效路径的相册库')
else:
    print('  ✅ 路径均正常')
" 2>/dev/null
fi

# ── 5. 显示当前相册库配置 ────────────────────────────────────
echo ""
echo "📚 当前相册库："
python3 -c "
import json
with open('$LIBRARY_FILE', 'r') as f:
    libs = json.load(f)
for lib in libs:
    status = '✅' if lib.get('enabled') else '❌'
    print(f'  {status} {lib[\"name\"]} → {lib[\"path\"]}')
" 2>/dev/null || echo "  (无法读取配置)"

# ── 6. 启动服务 ──────────────────────────────────────────────
echo ""
echo "🚀 启动相册服务..."
exec python3 server.py
