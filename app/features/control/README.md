# 服务控制台

一个进程托管器 + 一张 3×3 方形看板。目标是替代 `一键启动全部.bat` 的九个
`cmd /k` 窗口：服务由控制台用 `CREATE_NO_WINDOW` 拉起，桌面只留一个浏览器页面。

## 文件

- `specs.py`：九个服务的启动命令、端口、PID 文件、是否自动启动。
- `manager.py`：启动/停止/重启、状态刷新、按 PID/进程树清理、日志落盘。
- `panel.py`：5177 的 Flask 控制台，只监听 `127.0.0.1`。
- `web/control_panel.html`：看板页面。
- `启动控制台.bat`：一键入口，隐藏启动控制台并打开浏览器。

## 状态

- 绿：运行中（端口通了；SnowLuma/QQ 机器人还要求 OneBot 3000 通）。
- 黄：启动中/重启中/等待 QQ 登录。
- 红：已停止/异常。

颜色只做辅助，卡片上始终有文字标签，不靠颜色单独表达。

## 安全边界

- 控制台只 bind `127.0.0.1`，不对公网开放。
- API 需要 `X-Control-Token`，token 在页面加载时注入；别的网页拿不到。
- 所有子进程日志写到 `logs/control/<service>.log`，状态 PID 写到
  `state/control/<service>.pid`（`state/` 不入 git）。

## 手动 vs 自动

`agent`、`friend`、`admin`、两条隧道、`snowluma`、`qqbot` 默认自动启动；
`watchdog` 和 `comfyui` 默认手动。看门狗重启走隐藏助手
`app/workers/restart_services.py`，不再调用会开一排 cmd 窗口的 `一键启动全部.bat`；
ComfyUI 直接按 `comfy_launch` 的 python 命令启动，不走批处理。

## 运行

```bat
启动控制台.bat
```

入口脚本只做一件事：用 `pythonw` 隐藏启动 5177 控制台，随后立即退出。
浏览器由控制台进程启动 1.5 秒后自己打开；脚本里不再做端口探测、等待循环。
脚本保持纯 ASCII 注释，避免 cmd 在 UTF-8/GBK 编码下把中文命令吃掉导致闪退。

或：

```bat
pythonw.exe -m app.features.control.panel
```

打开 <http://127.0.0.1:5177>。
