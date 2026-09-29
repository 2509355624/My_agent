@echo off
setlocal
cd /d "%~dp0"
rem 这个标题是一键脚本识别并结束上一个实例的标记，不要改
title QQBOT-ADAPTER

echo ==================================================
echo   QQ 机器人适配层  ^(独立进程^)
echo ==================================================
echo.
echo   前置: NapCat 已在运行并完成登录
echo         启动命令 D:\AI\NapCat\启动NapCat.bat
echo.
echo   本窗口持续输出日志，请勿关闭。
echo   NapCat 重启后本进程会自动重连，无需手动重启。
echo.

if not exist "D:\AI\confyui_env\Scripts\python.exe" (
    echo [ERROR] 找不到 Python:
    echo         D:\AI\confyui_env\Scripts\python.exe
    pause
    exit /b 1
)

D:\AI\confyui_env\Scripts\python.exe -m app.qq_bot
set "RC=%ERRORLEVEL%"

echo.
echo [进程已退出] 退出码 %RC%
if not "%RC%"=="0" echo              ^(非 0 说明启动或运行出错，看上面的日志^)
echo.
echo   提示: 若提示「已有实例在运行」，说明旧进程还在，请用
echo         一键启动QQ机器人.bat 重启（它会先清理旧进程）。
echo.
pause
