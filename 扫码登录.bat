@echo off
setlocal
title QQ-SCAN-LOGIN
set "PY=D:\AI\confyui_env\Scripts\python.exe"
rem ---------------------------------------------------------------------------
rem Draws the QR code right in this window (that is how NapCat used to look,
rem but its own console stays silent as long as ANY account on this machine
rem still holds a login ticket - see napcat_qr.py header for the source line).
rem First it tries the saved ticket, only then does it ask for a scan.
rem ---------------------------------------------------------------------------
"%PY%" "%~dp0napcat_qr.py" --tries 3 --wait 150
echo.
pause
