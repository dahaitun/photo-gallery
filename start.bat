@echo off
chcp 65001 >nul
REM 启动私人相册服务（Windows）
REM 用法：双击运行，或在命令行执行 start.bat

cd /d "%~dp0"

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo 未找到 Python，请先安装 Python 3.9+
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

REM 创建虚拟环境（首次）
if not exist ".venv" (
    echo 首次运行：创建虚拟环境并安装依赖...
    python -m venv .venv
    .venv\Scripts\pip install -q --upgrade pip
    .venv\Scripts\pip install -q -r requirements.txt
    echo 依赖安装完成
)

echo.
echo 📸 私人相册服务启动中...
echo    本机访问：http://localhost:8080
echo    按 Ctrl+C 停止服务
echo.

.venv\Scripts\python server.py
pause
