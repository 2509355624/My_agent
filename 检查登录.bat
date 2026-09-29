@echo off
setlocal
title QQ-LOGIN-STATUS
set "PY=D:\AI\confyui_env\Scripts\python.exe"
rem Read-only: logged in? ticket saved? which accounts hold one?
rem Changes nothing. Run scan_login.bat (É¨ÂëµÇÂ¼.bat) if there is no ticket.
"%PY%" "%~dp0napcat_qr.py" --check
echo.
pause
