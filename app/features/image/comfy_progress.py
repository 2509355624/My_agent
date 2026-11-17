"""ComfyUI 采样进度监听（WebSocket）。

## 它解决什么

12GB 显存 + 16GB 内存跑 MOSS（底模 5.1GB + 文本编码器 6.9GB ≈ 12GB）本来就
贴着上限。跑过一两张之后内存被吃干，权重开始反复换页——**采样不会报错，只会
变慢**：单步从 1.5~2.1 s/it 掉到 10.8，最坏 48 s/it，50 步就是 25 分钟。

这一路之前没有任何信号：`/history` 要等出图才有东西，`/queue` 只说「在跑」，
`task_timeout` 是几百秒的粗兜底。结果就是用户盯着进度条干等 20 分钟，最后
还得靠自己发现「重启一下就好了」。

这个模块只做一件事：把 ComfyUI 通过 WebSocket 推的 `progress` 消息
（`{value, max, prompt_id, node}`）记下来，让上层能算出**当前这一步几秒**
（见 `sit`）。判断和处置都留给上层（`image_jobs.wait_done`）。

## 关键取舍

- **只检测事实，不做决定。** 这里不设阈值、不中断任务，只回答「最近一步多少秒」。
  阈值放在 `image_jobs`（渠道耗时是那边的知识），中断与文案也在那边。
- **拿不到数据就沉默。** 连不上、超时、模块缺失、ComfyUI 换成别的客户端占用
  了 `client_id`……任何一种情况都只让 `sit()` 返回 None，绝不猜、绝不误伤。
  没有进度可看最多是回到「没有这个功能」，误判却会砍掉一张正在正常出的图。
- **`client_id` 必须和提交时一致。** nightly 的 `progress` 消息是定向推给
  发起客户端的（`server.py` 里 `send_sync(..., client_id)`），所以
  `image_jobs._queue_prompt` 和这里共用同一个进程级 id（`client_id()`）。
- **只保留最近 4 个采样点。** s/it 用相邻两点算（Δ秒 / Δ步），留 4 个足够
  覆盖一次抖动，也不至于让长跑任务的字典无限长大。
"""

import collections
import json
import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)

try:                                    # 没装 websocket-client 就整体降级
    import websocket                    # type: ignore
except Exception:                       # noqa: BLE001
    websocket = None

# 进程级 client_id：提交任务和监听进度必须用同一个，否则收不到 progress。
_CLIENT_ID = "agent_" + uuid.uuid4().hex[:8]

# 每个 prompt 留几个采样点：(value, 时间戳)
_KEEP = 4

# 断线重连的等待（秒），封顶别让它变成忙等
_RECONNECT_MIN = 2.0
_RECONNECT_MAX = 30.0


def client_id():
    """提交任务时用这个 client_id，进度才会推给我们。"""
    return _CLIENT_ID


class _Watcher:
    def __init__(self, url):
        self.url = url
        self._lock = threading.Lock()
        self._samples = {}              # prompt_id -> deque[(value, ts)]
        self._thread = None
        self._stop = threading.Event()

    # ---- 生命周期 ----------------------------------------------------
    def start(self):
        if websocket is None:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="comfy-progress", daemon=True)
            self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def _run(self):
        wait = _RECONNECT_MIN
        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(
                    self.url + "?clientId=" + _CLIENT_ID, timeout=10)
            except Exception as exc:        # noqa: BLE001  连不上就算了
                log.debug("进度 WS 连不上（%s），%.0fs 后重试", exc, wait)
                if self._stop.wait(wait):
                    return
                wait = min(wait * 2, _RECONNECT_MAX)
                continue
            wait = _RECONNECT_MIN
            try:
                ws.settimeout(1.0)
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception:       # noqa: BLE001  超时/断开
                        if not self._alive(ws):
                            break
                        continue
                    self._handle(raw)
            finally:
                try:
                    ws.close()
                except Exception:           # noqa: BLE001
                    pass

    def _alive(self, ws):
        """recv 抛异常时分辨「只是超时」还是「连接断了」。"""
        try:
            return bool(ws.connected)
        except Exception:                   # noqa: BLE001
            return False

    # ---- 消息处理 ----------------------------------------------------
    def _handle(self, raw):
        if not isinstance(raw, str):
            return                          # 预览图是二进制帧，不要
        try:
            msg = json.loads(raw)
        except Exception:                   # noqa: BLE001
            return
        if msg.get("type") != "progress":
            return
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        value = data.get("value")
        if not pid or value is None:
            return
        with self._lock:
            q = self._samples.get(pid)
            if q is None:
                q = self._samples[pid] = collections.deque(maxlen=_KEEP)
            q.append((float(value), time.time()))

    # ---- 给上层的读数 ------------------------------------------------
    def sit(self, prompt_id):
        """最近一步用了几秒；样本不够或没有进度时返回 None。

        用**相邻两个采样点**算：`Δ秒 / Δ步`。ComfyUI 每步推一次，所以正常
        情况下 Δ步 = 1，读数就是单步耗时；跳步（极少）也照样算得对。
        """
        with self._lock:
            q = self._samples.get(prompt_id)
            if not q or len(q) < 2:
                return None
            (v0, t0), (v1, t1) = q[-2], q[-1]
        dv = v1 - v0
        if dv <= 0:
            return None
        return (t1 - t0) / dv

    def forget(self, prompt_id):
        with self._lock:
            self._samples.pop(prompt_id, None)


_watcher = None
_watcher_lock = threading.Lock()


def ensure_started(comfy_url):
    """按需拉起监听线程（同一个进程只拉一次）。返回是否可用。"""
    global _watcher
    if websocket is None:
        return False
    with _watcher_lock:
        if _watcher is None:
            url = comfy_url.replace("https://", "wss://").replace("http://", "ws://")
            _watcher = _Watcher(url.rstrip("/") + "/ws")
        return _watcher.start()


def sit(prompt_id):
    """最近一步耗时（秒）；没有数据返回 None。"""
    w = _watcher
    return w.sit(prompt_id) if w is not None else None


def forget(prompt_id):
    w = _watcher
    if w is not None:
        w.forget(prompt_id)
