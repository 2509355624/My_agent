"""服务总控面板的数据源：一页看全部服务的死活 + 日志尾部。

背景（2026-10-25 用户要求）：机器上常驻五六个服务（Agent 网页 / QQ 适配层 /
协议端 / 看门狗 / ComfyUI / llama.cpp），原来每个服务一个 cmd 窗口，看门狗每重启
一次就闪出一堆新窗口，用户要的是一个入口全看到。落地 = 本模块 + 5174 上的
`/services` 页面。

设计要点：
- **判据全部沿用现有实现，不发明新探针**：ComfyUI 用 `comfy_status.snapshot()`
  （网页状态后台同款，带 20 秒 TTL 缓存）；看门狗用 `tasklist` 窗口标题——跟
  `一键启动全部.bat` [4/4] 段判「WATCHDOG 窗口在不在」完全一致；其余 TCP 探
  127.0.0.1 端口，思路同 `watchdog._port_open`。
- **日志归集不搬家**：各进程的日志文件已在 `logs/` 下（logsetup 按进程名分文件，
  两个进程同写一个文件在 Windows 上会写烂行——所以这里只读、不让谁改目录），
  本模块只负责 tail 尾部。ComfyUI 的日志在它自己的 user/ 目录（项目外），路径
  走 config 的 `COMFYUI_LOG`，不存在就显示「无日志文件」，不报错。
- 探测全部 1~2 秒超时 + 全异常吞掉按「未知」处理：这个面板自己崩了不该比
  被探测的服务更惹眼。
"""

import logging
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

from app.config import (BASE_DIR, COMFYUI_LOG, LLAMA_BASE_URL, WEBUI_PORT)

log = logging.getLogger(__name__)

LOG_DIR = os.path.join(BASE_DIR, "logs")

ONEBOT_PORT = 3000            # QQ 适配层连协议端的 OneBot HTTP 口
_TCP_TIMEOUT = 1.0            # TCP 探活超时（秒）——面板在轮询，必须快
_TAIL_LINES = 60              # 每个日志默认带多少行
_TAIL_MAX_BYTES = 64 * 1024   # 最多从文件尾读这么多字节（5MB 的日志也不能整篇拉）


def tail(path, lines=_TAIL_LINES, max_bytes=_TAIL_MAX_BYTES):
    """读日志尾部若干行。文件不存在/读不了返回 None；空文件返回 []。

    从文件尾倒读固定字节数，不整篇 load——agent.log 有 4MB 级，轮询页面
    每秒拉一次整篇会把 5174 自己拖慢。中间截断的首行可能不完整，允许
    （日志面板看的是最新几行，不是做取证）。
    """
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]


def _log_block(path):
    """日志文件 → 面板用的 dict。读不到不是错误，是没有。"""
    if not path:
        return {"path": "", "ok": False, "age": None, "lines": []}
    lines = tail(path)
    if lines is None:
        return {"path": path, "ok": False, "age": None, "lines": []}
    try:
        age = round(time.time() - os.path.getmtime(path))
    except OSError:
        age = None
    return {"path": path, "ok": True, "age": age, "lines": lines}


def _tcp_alive(port):
    """127.0.0.1:port 通不通。异常一律 False（探测失败≠服务正常，宁可误报离线）。

    构造也放进 try：socket() 本身失败（句柄耗尽那类）时不该把异常漏给调用方
    ——面板上一次探测抖动不该把整页数据源搞挂。
    """
    s = None
    try:
        s = socket.socket()
        s.settimeout(_TCP_TIMEOUT)
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        if s is not None:
            s.close()


def _watchdog_alive():
    """看门狗在不在——判据同 `一键启动全部.bat` [4/4]：WATCHDOG 窗口的 cmd 还在。

    不按 PID/锁文件：锁文件在进程被杀后不会自己消失（Windows 文件锁随进程
    释放，但文件本体还在），按它判会把「上次意外死掉的看门狗」算成活的。

    ⚠️ 输出按 **bytes** 匹配、不开 text=True：tasklist 在中文 Windows 上按
    GBK 输出，text=True 会按 UTF-8 解码，reader 线程直接 UnicodeDecodeError
    （异常不打进返回值，run() 照样返回、stdout 却缺斤少两）——要找的
    "cmd.exe" 是 ASCII，字节匹配零解码，顺带免疫代码页差异。
    """
    if sys.platform != "win32":
        return None
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "WINDOWTITLE eq WATCHDOG", "/NH"],
            capture_output=True, timeout=5)
        return b"cmd.exe" in (out.stdout or b"")
    except Exception:
        return None


def _comfy_entry():
    """ComfyUI：探活复用网页状态后台那个带 TTL 缓存的 snapshot，不另起探针。

    snapshot 只回 online/running/pending，没有显存——显存余量单独补一次
    /system_stats（本地接口很轻；拿不到就只报队列，不为难面板）。
    返回 (online, detail) 元组，与其它 probe 一致（add() 按元组解包）。
    """
    from app import comfy_status
    try:
        c = comfy_status.snapshot()
    except Exception as exc:
        return None, "探测失败：%r" % (exc,)[:120]
    if not c.get("online"):
        return False, "8188 无响应"
    detail = "running %s / pending %s" % (c.get("running"), c.get("pending"))
    try:
        import requests
        from app.config import COMFYUI_URL
        r = requests.get(COMFYUI_URL + "/system_stats", timeout=2)
        r.raise_for_status()
        devs = r.json().get("devices") or []
        if devs:
            detail += " · vram_free %.1fGB" % (
                (devs[0].get("vram_free") or 0) / 2 ** 30)
    except Exception:
        pass
    return True, detail


def _bot_entry():
    """QQ 适配层：OneBot :3000 通 = 活着；再带上 qq_status 快照的新鲜度。"""
    online = _tcp_alive(ONEBOT_PORT)
    detail = ":3000 %s" % ("监听中" if online else "无监听")
    try:
        from app import qq_status
        snap, stale = qq_status.read()
        if stale:
            detail += " · 状态快照已停更（进程可能卡死）"
        elif snap:
            detail += " · 心跳正常"
    except Exception:
        pass
    return online, detail


def _llama_port():
    """从 LLAMA_BASE_URL 解析端口，解析不了退回 8081（config 的默认值）。"""
    try:
        return urlparse(LLAMA_BASE_URL).port or 8081
    except Exception:
        return 8081


def snapshot():
    """整份面板数据。任何单项炸掉都不该拖垮整页——逐项 try。"""
    try:
        llama_port = _llama_port()
    except Exception:
        llama_port = 8081

    entries = []

    def add(sid, name, desc, port, probe, logpath=""):
        try:
            online, detail = probe()
        except Exception as exc:
            online, detail = None, "探测异常：%r" % (exc,)[:120]
        entries.append({
            "id": sid, "name": name, "desc": desc,
            "port": port, "online": online, "detail": detail,
            "log": _log_block(logpath),
        })

    add("web", "Agent 网页", "本服务（5174）· 聊天/生图台/漫画台", 5174,
        lambda: (True, "能返回本接口就是活着"),
        os.path.join(LOG_DIR, "agent.log"))

    add("bot", "QQ 适配层", "qq_bot · 收发消息 / 生图回执", ONEBOT_PORT,
        _bot_entry, os.path.join(LOG_DIR, "qq_bot.log"))

    add("proto", "协议端", "NapCat / SnowLuma · 扫码登录态",
        WEBUI_PORT, lambda: (_tcp_alive(WEBUI_PORT),
                             "WebUI :%d %s" % (WEBUI_PORT,
                                               "监听中" if _tcp_alive(WEBUI_PORT)
                                               else "无监听")),
        "")

    add("watchdog", "看门狗", "心跳守护 · 掉线自动重启", None,
        lambda: (_watchdog_alive(), "窗口标题 WATCHDOG %s" % (
            "在" if _watchdog_alive() else "不在")),
        os.path.join(LOG_DIR, "watchdog.log"))

    add("comfy", "ComfyUI", "生图后端 · sf/silver/moss 全走这", 8188,
        _comfy_entry, COMFYUI_LOG)

    add("llama", "llama.cpp", "本地主对话 + 识图引擎", llama_port,
        lambda: (_tcp_alive(llama_port),
                 ":%d %s" % (llama_port,
                             "监听中" if _tcp_alive(llama_port) else "无监听")),
        "")

    return {"ts": time.time(), "services": entries}
