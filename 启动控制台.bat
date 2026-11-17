@echo off
setlocal

rem Force project root. If this .bat is copied to Desktop/Downloads, %~dp0
rem is not the project and "pythonw -m app..." cannot find the app package.
set "ROOT=D:\AI\my_agent_restructure"
if not exist "%ROOT%\app\features\control\panel.py" set "ROOT=%~dp0"
cd /d "%ROOT%"

rem Start the hidden 5177 control panel. The panel opens the browser itself.
rem No port probing / wait loop here: every extra command is another way to
rem make the console window vanish before the panel starts.
set "PYW="
if exist "D:\AI\my_env\Scripts\pythonw.exe" set "PYW=D:\AI\my_env\Scripts\pythonw.exe"
if not defined PYW if exist "D:\AI\confyui_env\Scripts\pythonw.exe" set "PYW=D:\AI\confyui_env\Scripts\pythonw.exe"
if not defined PYW if exist "D:\AI\comfy_env\Scripts\pythonw.exe" set "PYW=D:\AI\comfy_env\Scripts\pythonw.exe"
if not defined PYW set "PYW=pythonw.exe"

if not exist "%PYW%" goto missing
start "" "%PYW%" -m app.features.control.panel
exit /b 0

:missing
echo [ERROR] pythonw.exe not found
echo Check D:\AI\confyui_env\Scripts\pythonw.exe
pause
exit /b 1
