"""ComfyUI 实时状态探测——给状态栏（prompt 尾部）用的唯一事实来源。

背景（2026-09-28）：ComfyUI 凌晨死进程后，机器人没有实时状态来源，只能靠
历史记忆回答「生图掉线了吗」，说错了也无从纠正。把探测结果放进每轮追加在
**最尾部**的状态栏（app/agent_prompt.build_status_bar），状态变化只 miss
尾部那一小段，历史前缀不受影响——与既有状态栏的 time: 字段同一代价。

设计要点：
- 探测结果按 TTL 缓存在进程内：一轮对话可能多次拼状态栏（QQ + 网页、
  多个会话同轮），20 秒内的重复拼接直接复用，不打爆 ComfyUI。
- 出网口一律 trust_env=False（本机 Clash 写注册表代理，不清干净会把
  127.0.0.1 也劫持进死代理，见 MEMORY.md）。所有异常吞掉并按「离线」
  处理——状态栏宁可保守说掉线，也不能让探测本身炸掉一轮对话。
- 测试约定：patch `comfy_status.snapshot`（纯内存返回值），别让测试
  走真网络；需要测探测逻辑本身时 patch `_session`。
"""

import time

import requests

from app.config import COMFYUI_URL

# 与 llm/qq_api/vision 等同一套规矩：完全不理会环境与注册表代理
_session = requests.Session()
_session.trust_env = False

_PROBE_TIMEOUT = 2          # 秒；状态栏是每轮必经路径，探测必须快
_TTL = 20.0                 # 秒；缓存窗口

# {"ts": monotonic, "online": bool|None, "running": int, "pending": int}
_CACHE = {"ts": 0.0, "online": None, "running": 0, "pending": 0}


def _probe():
    """真探测。返回 (online, running, pending)；任何异常都算离线。"""
    try:
        resp = _session.get(COMFYUI_URL + "/system_stats",
                            timeout=_PROBE_TIMEOUT)
        resp.raise_for_status()
    except Exception:
        return (False, 0, 0)
    running = pending = 0
    try:
        q = _session.get(COMFYUI_URL + "/queue",
                         timeout=_PROBE_TIMEOUT).json()
        running = len(q.get("queue_running") or [])
        pending = len(q.get("queue_pending") or [])
    except Exception:
        # /system_stats 通了就算在线，队列数字拿不到就按 0 报
        pass
    return (True, running, pending)


def snapshot(force=False):
    """取 ComfyUI 状态快照（带 TTL 缓存）。"""
    now = time.monotonic()
    if (not force and _CACHE["online"] is not None
            and now - _CACHE["ts"] < _TTL):
        return dict(_CACHE)
    online, running, pending = _probe()
    _CACHE.update({"ts": now, "online": online,
                   "running": running, "pending": pending})
    return dict(_CACHE)


def reset_cache():
    """清缓存（测试用；生产里不需要——TTL 到期自然重探）。"""
    _CACHE.update({"ts": 0.0, "online": None, "running": 0, "pending": 0})


def status_line():
    """给状态栏的那一行。措辞直接给模型下指令，别让它猜。"""
    s = snapshot()
    if s["online"]:
        if s["running"] or s["pending"]:
            return ("comfyui: online；正在画 %d 张、排队 %d 张"
                    % (s["running"], s["pending"]))
        return "comfyui: online；画图队列空闲，可以接画图请求"
    return ("comfyui: OFFLINE（%s 连不上）——不要答应画图请求，工具会失败；"
            "对方要图就直说画图服务暂时离线、稍后再试" % COMFYUI_URL)
