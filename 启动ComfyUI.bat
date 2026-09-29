@echo off
cd /d "%~dp0"
title COMFYUI

rem ComfyUI 启动器。
rem   手动：双击本文件
rem   看门狗：`启动ComfyUI.bat auto`（隐藏窗口跑，跑完不等回车）
rem 命令来源 = D:\AI\启动手册.txt 第一行，一字不改照抄。

set "AUTO_RUN="
if /i "%~1"=="auto" set "AUTO_RUN=1"

echo ==================================================
echo   ComfyUI  http://127.0.0.1:8188
echo ==================================================
echo.

if not exist "D:\AI\confyui_env\Scripts\python.exe" (
    echo [ERROR] 找不到 D:\AI\confyui_env\Scripts\python.exe
    if not defined AUTO_RUN pause
    exit /b 1
)
if not exist "D:\AI\ComfyUI_v037\main.py" (
    echo [ERROR] 找不到 D:\AI\ComfyUI_v037\main.py
    if not defined AUTO_RUN pause
    exit /b 1
)

rem ⚠️ 这里**不 taskkill**。看门狗是「探活连续 3 次失败」才叫到这里，
rem 那会儿进程已经没了，杀不杀都一样；反过来万一探活误判（ComfyUI 正在
rem 自己重启、或只是卡了一下），一杀就是把正在跑的图连进程一起干掉。
rem 端口被占时 ComfyUI 自己会报 bind 失败并退出，不会开成两个。

D:\AI\confyui_env\Scripts\python.exe D:\AI\ComfyUI_v037\main.py --listen 127.0.0.1 --port 8188 --enable-cors-header "*"

echo.
echo [ComfyUI 已退出] 退出码 %ERRORLEVEL%
if not defined AUTO_RUN pause
