@echo off
cd /d "%~dp0"
title WATCHDOG
D:\AI\confyui_env\Scripts\python.exe -m app.watchdog
echo [看门狗已退出] 退出码 %ERRORLEVEL%
pause
