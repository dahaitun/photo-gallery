FROM python:3.11-slim

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安装 Python 依赖（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 给启动脚本执行权限
RUN chmod +x /app/entrypoint.sh

# 数据持久化
VOLUME ["/app/data"]

# 缩略图缓存目录
RUN mkdir -p /app/data/cache

EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/info')" || exit 1

# 使用启动脚本
CMD ["/app/entrypoint.sh"]
