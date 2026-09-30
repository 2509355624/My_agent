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

rem [!] 这里**不 taskkill**。看门狗是「探活连续 3 次失败」才叫到这里，
rem 那会儿进程已经没了，杀不杀都一样；反过来万一探活误判（ComfyUI 正在
rem 自己重启、或只是卡了一下），一杀就是把正在跑的图连进程一起干掉。
rem 端口被占时 ComfyUI 自己会报 bind 失败并退出，不会开成两个。

rem [!!] 必须先 cd 进 ComfyUI 自己的目录。models/ output/ user/ input/ 全是按
rem      当前工作目录找的 —— 在 D:\AI\My_agent 下启动会开出一个「空」ComfyUI：
rem      没有模型、没有 anime2 工作流，而且它会占着 8188 让人以为一切正常。
cd /d "%CFY_DIR%"

rem 参数 = run_comfyui.bat 那一套，一个不改：
rem   --vram-headroom 1  给系统留 1GB 显存（DynamicVRAM 用）
rem   --force-fp16       张量内存减半
rem   --cache-none       尽量不缓存，省 RAM/VRAM（这台机器 16GB 内存，值得）
rem 额外显式钉死 --listen 127.0.0.1 --port 8188，跟 .env 的 COMFYUI_URL 对齐。
rem   其实这也是 ComfyUI 的默认值，写出来只是防止哪天默认值变了没人发现。
rem --enable-cors-header "*" 沿用本文件原来的写法：agent 网页端(5174)如果要直接
rem   用 JS 拉 8188 的图，没这个头会被浏览器拦掉。留着不碍事。
rem
rem [!] 别被 app/image_jobs.py 开头那句「--vram-headroom 反而拖垮了整机」骗了：
rem    那条结论是 2026-09-27 针对 qwen_image_v1（一套权重 10.5GB）的，那个渠道
rem    已经在 app/config.py 的 DISABLED_IMAGE_SKILLS 里停用。anima/SD/anime2
rem    用这套参数是实测跑得动的（run_comfyui.bat 天天在用）。**别顺手把参数删了。**
"%PY%" main.py --listen 127.0.0.1 --port 8188 --enable-cors-header "*" --vram-headroom 1 --force-fp16 --cache-none

echo.
echo [ComfyUI 已退出] 退出码 %ERRORLEVEL%
if not defined AUTO_RUN pause
