"""ComfyUI 实时状态探测 + 状态栏的 NAI 队列行。

背景（2026-09-28）：ComfyUI 凌晨死进程后，机器人没有实时状态来源，只能靠
历史记忆回答「生图掉线了吗」，说错了也无从纠正。当时的做法是把探测结果放进
每轮追加在**最尾部**的状态栏（app/agent_prompt.build_status_bar）。

2026-09-29 改：**状态栏不再报 ComfyUI 的死活**。用户的要求是「模型只需要知道
任务已经推送到排队队列」——入队回执由 generate_image 的返回值给，跑完的回执由
app/image_jobs.recent_line 给，模型不需要、也不该从状态栏去猜本机画图服务在不在。
所以 `snapshot()` 现在只服务网页状态后台（app/main.py 的 /api/status），状态栏
那行只剩 `nai_line()`——它读的是 agent 侧自己的队列，不是 ComfyUI 的状态。

设计要点：
- 探测结果按 TTL 缓存在进程内：状态后台 2 秒轮询一次，20 秒内的重复请求直接
  复用，不打爆 ComfyUI。
- 出网口一律 trust_env=False（本机 Clash 写注册表代理，不清干净会把
  127.0.0.1 也劫持进死代理，见 MEMORY.md）。所有异常吞掉并按「离线」
  处理——状态后台宁可保守说掉线，也不能让探测本身炸掉请求。
- 测试约定：patch `comfy_status.snapshot`（纯内存返回值），别让测试
  走真网络；需要测探测逻辑本身时 patch `_session`。
"""

import time

import requests

from app.config import COMFYUI_URL

# 与 llm/qq_api/vision 等同一套规矩：完全不理会环境与注册表代理
_session = requests.Session()
_session.trust_env = False

_PROBE_TIMEOUT = 2          # 秒；状态后台在轮询，探测必须快
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


def nai_line():
    """状态栏里 NAI 的那一行。

    队列有活就报数字，空闲也要明说——机器人对「NAI 跑完没有」的判断全靠这行，
    不说死它就会去猜。注意这是 **agent 侧自己的队列**（image_jobs），不是
    ComfyUI 的状态：NAI 是云端调用，ComfyUI 那边根本看不见。
    """
    from app import image_jobs     # 局部导入：image_jobs 较重，按需拉起
    running, pending = image_jobs.nai_depth()
    if running or pending:
        return ("nai: 正在画 %d 张、排队 %d 张（NovelAI 云端，与本机 ComfyUI"
                " 无关）" % (running, pending))
    return "nai: 空闲（没有在画的 NAI 图）"
