@echo off
cd /d "%~dp0"
title SNOWLUMA

rem ===========================================================================
rem  SnowLuma 协议端启动器（NapCat 的替代品，2026-09-30 起可切换）
rem
rem  和 启动NapCat.bat 的关键区别：**这里不 taskkill 任何东西**。
rem  NapCat 必须杀掉旧 QQ.exe 再注入，SnowLuma 是「发现 QQ.exe 就注入 hook」，
rem  它自己会处理已运行的 QQ —— 抢着杀反而会把登录态弄乱。
rem
rem  数据目录（config/ data/）是**相对当前工作目录**的，所以必须先 cd 进来。
rem  这也是为什么这里用 pushd 而不是直接调 D:\AI\SnowLuma\launcher.bat。
rem
rem  首次启动：控制台会打印一次性 WebUI 临时密码，浏览器开
rem  http://127.0.0.1:5099 登录，在里面配 OneBot 并扫码。
rem ===========================================================================

set "SL_DIR=D:\AI\SnowLuma"
set "SL_NODE=%SL_DIR%\node.exe"
set "SL_MAIN=%SL_DIR%\index.mjs"

rem auto = 看门狗调起来的（隐藏窗口跑）：跑完直接退出，不等回车
set "AUTO_RUN="
if /i "%~1"=="auto" set "AUTO_RUN=1"

if not exist "%SL_NODE%" (
    echo [ERROR] 找不到 "%SL_NODE%"
    echo         SnowLuma 未安装或路径不对（应从 GitHub Releases 解压到 %SL_DIR%）
    if not defined AUTO_RUN pause
    exit /b 1
)
if not exist "%SL_MAIN%" (
    echo [ERROR] 找不到 "%SL_MAIN%"
    echo         解压不完整，重新下载 SnowLuma-v*-win-x64.zip 解压到 %SL_DIR%
    if not defined AUTO_RUN pause
    exit /b 1
)

echo ==================================================
echo   SnowLuma 启动器  ^(OneBot 11^)
echo ==================================================
echo.
echo   WebUI:  http://127.0.0.1:5099
echo   WS:     127.0.0.1:3001   ^(事件上行^)
echo   HTTP:   127.0.0.1:3000   ^(消息下发^)
echo.
echo   提示: 3000 / 3001 只在 QQ 登录成功后才监听，
echo         登录前只有 5099，属正常现象。
echo   提示: SnowLuma 会自动发现并注入正在运行的 QQ.exe，
echo         所以要保证 QQ 已经开着（或它起来后自己会拉）。
echo.

echo [*] 启动中... 登录后本窗口会持续输出日志，请勿关闭。
echo     首次登录：浏览器打开 http://127.0.0.1:5099 ，
echo     用控制台打印的一次性临时密码登录，在里面扫码。
echo.

pushd "%SL_DIR%"
"%SL_NODE%" "%SL_MAIN%"
popd

if not defined AUTO_RUN pause
