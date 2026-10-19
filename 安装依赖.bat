@echo off
setlocal
cd /d "%~dp0"
title AGENT-SETUP

echo ==================================================
echo   安装依赖（只需跑一次）
echo ==================================================
echo.

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [X] 没找到 Python。
  echo     装一个 Python 3.10 以上，安装时勾选 "Add python.exe to PATH"，再双击这个。
  echo.
  pause
  exit /b 1
)

echo 找到 Python：
"%PY%" -c "import sys; print('   ' + sys.version.split()[0] + '  ' + sys.executable)"

echo.
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 创建虚拟环境 .venv ...
  "%PY%" -m venv .venv
  if errorlevel 1 (
    echo [X] 虚拟环境创建失败。
    pause
    exit /b 1
  )
) else (
  echo [1/3] 已有 .venv，跳过
)

set "VPY=%~dp0.venv\Scripts\python.exe"

echo [2/3] 升级 pip ...
"%VPY%" -m pip install --upgrade pip -q

echo [3/3] 安装依赖（首次 1~3 分钟）...
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [X] 安装失败。如果是网络问题，换国内源重试：
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  echo.
  pause
  exit /b 1
)

echo.
echo ==================================================
echo   装好了，现在双击 一键启动.bat
echo ==================================================
echo   可选：RAG 知识库（约 2~3 GB，不装不影响助手和生图）
echo   .venv\Scripts\python.exe -m pip install -r requirements-rag.txt
echo.
pause
