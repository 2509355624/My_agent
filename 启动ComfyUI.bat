@echo off
cd /d "%~dp0"
title COMFYUI

rem ComfyUI 启动器。
rem   手动：双击本文件
rem   看门狗：`启动ComfyUI.bat auto`（隐藏窗口跑，跑完不等回车）
rem 参数抄自 D:\AI\run_comfyui.bat —— 那是用户平时手点的那份，实测跑得动。
rem   [!!] 不直接调 run_comfyui.bat：它结尾有 pause，看门狗是隐藏窗口拉起来的，
rem        挂在那儿没人按回车，进程就永远退不掉。所以只抄参数，不抄调用。
rem        改参数请两边一起改（两份文件都在 D:\AI 下，不归 git 管）。
rem 路径不写死，按机器自动挑：desktop = comfy_env / ComfyUI，laptop = confyui_env
rem / ComfyUI_v037。2026-09-30 之前这里写死的是 laptop 那套，在台式机上直接
rem 报「找不到 D:\AI\confyui_env\Scripts\python.exe」，看门狗叫它也叫不动。

set "AUTO_RUN="
if /i "%~1"=="auto" set "AUTO_RUN=1"

echo ==================================================
echo   ComfyUI  http://127.0.0.1:8188
echo ==================================================
echo.

rem ── venv python ──
rem [!!] 这里**不能**照抄 一键启动全部.bat 的顺序。那个 PY 是给机器人/napcat_qr
rem      用的，首选 D:\AI\my_env（agent 的 venv，没有 torch）。ComfyUI 必须用
rem      自己那套带 torch 的环境，所以 my_env 根本不参与挑选。
set "PY="
if not defined PY if exist "D:\AI\comfy_env\Scripts\python.exe" set "PY=D:\AI\comfy_env\Scripts\python.exe"
if not defined PY if exist "D:\AI\confyui_env\Scripts\python.exe" set "PY=D:\AI\confyui_env\Scripts\python.exe"
if not defined PY set "PY=python"

rem ── ComfyUI 目录 ──
set "CFY_DIR="
rem [!!] 2026-10-28 主实例已换到 nightly（moss 系工作流只在 nightly 的节点集里能跑）。
rem      nightly 必须排第一位：v037 认不出 UNETLoader + moss_image 那套节点，
rem      而且缺 --disable-comfy-compiler 直接 aimdo memory compile error 起不来。
if not defined CFY_DIR if exist "D:\AI\ComfyUI-nightly\main.py" set "CFY_DIR=D:\AI\ComfyUI-nightly"
if not defined CFY_DIR if exist "D:\AI\ComfyUI\main.py" set "CFY_DIR=D:\AI\ComfyUI"
if not defined CFY_DIR if exist "D:\AI\ComfyUI_v037\main.py" set "CFY_DIR=D:\AI\ComfyUI_v037"

set "PY_OK="
if exist "%PY%" set "PY_OK=1"
if /i "%PY%"=="python" set "PY_OK=1"
if not defined PY_OK (
    echo [ERROR] 找不到 ComfyUI 用的 python：%PY%
    if not defined AUTO_RUN pause
    exit /b 1
)
if not defined CFY_DIR (
    echo [ERROR] 找不到 ComfyUI 的 main.py（试过 D:\AI\ComfyUI 和 D:\AI\ComfyUI_v037）
    if not defined AUTO_RUN pause
    exit /b 1
)
echo   解释器 : %PY%
echo   工作目录 : %CFY_DIR%

rem 2026-10-28 改：手动启动（不带 auto 参数）时，8188 已被旧实例占用就先
rem 强杀再启动 —— 用户拍板的「先杀死旧的，然后再启动」。auto 模式（看门狗
rem 调用）保持原行为不杀：看门狗探活失败叫到这里时进程多半已经没了，杀了
rem 也白杀；反过来万一探活误判（ComfyUI 只是卡了一下），一杀就是把正在跑
rem 的图连进程一起干掉。端口没被占时下面的循环自然什么都不做。

rem [!!] 必须先 cd 进 ComfyUI 自己的目录。models/ output/ user/ input/ 全是按
rem      当前工作目录找的 —— 在 D:\AI\My_agent 下启动会开出一个「空」ComfyUI：
rem      没有模型、没有 anime2 工作流，而且它会占着 8188 让人以为一切正常。
cd /d "%CFY_DIR%"

rem ── 先杀死占用 8188 的旧实例（仅手动模式）──
if defined AUTO_RUN goto :no_kill
set "OLD_PID="
for /f "delims=" %%p in ('%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "(Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue).OwningProcess"') do set "OLD_PID=%%p"
if not defined OLD_PID goto :no_kill
echo   发现旧实例占用 8188（PID %OLD_PID%），先结束它...
taskkill /F /PID %OLD_PID% >nul 2>&1
set "TRY=0"
:wait_port
timeout /t 2 /nobreak >nul
set "STILL="
for /f "delims=" %%p in ('%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -Command "(Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue).OwningProcess"') do set "STILL=%%p"
if not defined STILL goto :no_kill
set /a TRY+=1
if %TRY% lss 5 goto :wait_port
echo   [WARN] 旧实例 10 秒后仍在（PID %STILL%），继续尝试启动
:no_kill

rem 参数 = run_comfyui.bat 那一套（2026-10-01 起两边一致）：
rem   --vram-headroom 1  给系统留 1GB 显存（DynamicVRAM 用）
rem   --force-fp16       张量内存减半
rem   [!!] 2026-10-01 去掉 --cache-none。理由见 run_comfyui.bat：那条是按
rem        「16GB 内存」调的，本机 32GB（run_comfyui.bat 的标题就是
rem        「6GB VRAM + 32GB RAM」），省 RAM 不值，而它让每次跑图每个节点都
rem        重算。**两份文件要一起改**，别只改一边。
rem 额外显式钉死 --listen 127.0.0.1 --port 8188，跟 .env 的 COMFYUI_URL 对齐。
rem   其实这也是 ComfyUI 的默认值，写出来只是防止哪天默认值变了没人发现。
rem --enable-cors-header "*" 沿用本文件原来的写法：agent 网页端(5174)如果要直接
rem   用 JS 拉 8188 的图，没这个头会被浏览器拦掉。留着不碍事。
rem
rem [!] 别被 app/image_jobs.py 开头那句「--vram-headroom 反而拖垮了整机」骗了：
rem    那条结论是 2026-09-27 针对 qwen_image_v1（一套权重 10.5GB）的，那个渠道
rem    已经在 app/config.py 的 DISABLED_IMAGE_SKILLS 里停用。anima/SD/anime2
rem    用这套参数是实测跑得动的（run_comfyui.bat 天天在用）。**别顺手把参数删了。**
rem [!] 参数**不在这里写死**：看门狗（watchdog._trigger_comfy_restart）和
rem     采样降级强杀（image_jobs._force_restart_comfy）都从 app/comfy_launch.py
rem     取命令。手动双击这份 bat 如果自己另写一套，就会出现「自动拉起能跑、
rem     手动拉起跑不动」的诡异差异。所以这里也去问它，问不到才用兜底。
set "CFY_ARGS="
for /f "delims=" %%a in ('"%PY%" -c "import sys;from app import comfy_launch;sys.stdout.write(' '.join(comfy_launch.launch_args()))" 2^>nul') do set "CFY_ARGS=%%a"
if not defined CFY_ARGS (
    echo   [WARN] 读不到 comfy_launch 的参数，用内置兜底（nightly 三件套）
    set "CFY_ARGS=--listen 127.0.0.1 --port 8188 --disable-comfy-compiler --enable-triton-backend --enable-cors-header *"
) else (
    echo   启动参数 : %CFY_ARGS%
)

rem 12GB 卡缺了 expandable_segments 很容易碎片化 OOM（app/comfy_launch.env() 同样补这一项）
if not defined PYTORCH_CUDA_ALLOC_CONF set "PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True"

"%PY%" main.py %CFY_ARGS%

echo.
echo [ComfyUI 已退出] 退出码 %ERRORLEVEL%
if not defined AUTO_RUN pause
