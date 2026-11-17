@echo off
setlocal
cd /d "%~dp0"

rem 一键启动服务控制台：一个浏览器界面，不再弹九个 cmd 窗口。
rem 控制台自己用 pythonw 隐藏运行；服务由它用 CREATE_NO_WINDOW 拉起。

rem 不能依赖 PATH：有些环境里 cmd 的 PATH 没有 System32，netstat/findstr
rem 会直接让批处理以 255 退出，看起来就是「闪退」。
if not defined SystemRoot set "SystemRoot=C:\Windows"
set "SYS32=%SystemRoot%\System32"
set "NETSTAT=%SYS32%\netstat.exe"
set "FINDSTR=%SYS32%\findstr.exe"
set "PING=%SYS32%\PING.EXE"

set "PYW="
if exist "D:\AI\my_env\Scripts\pythonw.exe" set "PYW=D:\AI\my_env\Scripts\pythonw.exe"
if not defined PYW if exist "D:\AI\confyui_env\Scripts\pythonw.exe" set "PYW=D:\AI\confyui_env\Scripts\pythonw.exe"
if not defined PYW if exist "D:\AI\comfy_env\Scripts\pythonw.exe" set "PYW=D:\AI\comfy_env\Scripts\pythonw.exe"
if not defined PYW set "PYW=pythonw.exe"

set "PORT=5177"
set "CONTROL_PORT=%PORT%"

rem 控制台已经在跑：只开浏览器，不重复起进程。
"%NETSTAT%" -ano 2>nul | "%FINDSTR%" ":%PORT% " | "%FINDSTR%" "LISTENING" >nul
if not errorlevel 1 goto open

start "" "%PYW%" -m app.features.control.panel

set /a WAIT_N=0
:wait
"%NETSTAT%" -ano 2>nul | "%FINDSTR%" ":%PORT% " | "%FINDSTR%" "LISTENING" >nul
if not errorlevel 1 goto open
set /a WAIT_N+=1
if %WAIT_N% geq 30 goto fail
"%PING%" -n 2 127.0.0.1 >nul
goto wait

:open
start "" "http://127.0.0.1:%PORT%"
exit /b 0

:fail
echo [ERROR] 控制台 30 秒内没起来。
echo         看一下 logs\control-panel.log，或手动运行：
echo         "%PYW%" -m app.features.control.panel
pause
exit /b 1
