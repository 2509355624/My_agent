"""生图任务队列：全局单通道串行，一次只让 ComfyUI 跑一张。

## 它解决什么

原先 submit() 的前提是「调用方已经 _queue_prompt 提交给 ComfyUI 了，这里只
登记一下」。也就是说**排队发生在 ComfyUI 内部**，而 agent 侧既看不见也管不着：

- **显存**：MAX_INFLIGHT 是「每个会话 2 张」，N 个会话并发就是 N×2 张灌进
  ComfyUI 队列。qwen_image_v1 单张峰值就要十几 GB（Q4_K 的 unet + 8B 文本
  编码器），塞进 12GB 显存全靠 offload 硬撑；连堆两张就会崩在
  ComfyUI-GGUF 的 Q4_K 反量化上，而且**会把整个 ComfyUI 一起带崩**。
- **超时**：任务已经在 ComfyUI 手里了，agent 侧只能「不等了」，没法把它摘
  出来——僵尸继续占着显存，后面的人一直等。
- **公平性**：ComfyUI 是先进先出，一个会话连点 5 张就能把所有人堵死。

## 做法

改成 agent 侧自己排：所有会话（QQ + 网页）都往**同一个 FIFO** 里放，由唯一
一个 worker 线程逐张喂给 ComfyUI。同一时刻 ComfyUI 里最多只有 1 个任务。

- 前一张跑完才提交下一张 → 显存不再叠加，从根上消除 OOM；
- 超时可以真取消：/interrupt + /queue delete + /free 三连，不留僵尸；
- 队列在 agent 侧，能数、能限长、能告诉对方「你排在第几」。

吞吐不会因此变差：ComfyUI 本来就是串行的（queue_running 恒为 1），这里只是
把排队位置从 ComfyUI 挪到我们这边——那边看不见，这边能。

## 关键取舍

- **超时从「真正开跑」算起**，不含排队时间。否则排在第 5 位的人还没轮到就被
  判超时了。代价是排队时间不可控，网页侧要一直等着（见 Job.wait）。
- **只在异常路径 /free**：正常跑完继续用同一个模型更快；而超时往往伴随显存
  已经被啃满，先清干净再让下一张上，免得连锁崩。
- **会话身份在入队那一刻快照**：qq_api 的上下文是线程本地的，worker 线程取
  不到，所以 target / target_id 必须随任务带过去（猜错就发错群）。
- **网页侧同步等，但等的时候不占 worker**：worker 只管跑图，网页请求线程自
  己在 Job.wait 上阻塞，两者分开。
"""

import collections
import logging
import threading
import time
import uuid

import requests

from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, IMAGE_GEN_TIMEOUT, QQ_AGENT_ID

log = logging.getLogger("image_jobs")

# 同一会话同时在途（含排队中）的张数上限：防一个人连环点单把队列占满。
MAX_INFLIGHT = 2

# 全局队列上限（含正在跑的那张）。十几个群同时刷图时，不能让队列无限长——
# 排到一小时之后的图，对方早就不看了。超了就让模型回一句「排队的人太多」。
MAX_QUEUE = 10

# 单张图从「真正开跑」到出图的时限（秒）。到点还没出图就中断它、让下一个上。
TASK_TIMEOUT = IMAGE_GEN_TIMEOUT

# 轮询间隔（秒）
POLL_INTERVAL = 2

_lock = threading.Lock()
_queue = collections.deque()      # 待跑的任务（不含正在跑的那个）
_running = None                   # 正在跑的任务（只为可观测 / 算排队位次）
_per_session = {}                 # (target, target_id) -> 在途张数（含排队）
_worker_started = False
_wake = threading.Event()         # 有新任务入队时戳一下 worker


class Job:
    """一次生图任务。

    QQ 侧提交完即返回（图由 worker 直接发回原会话）；网页侧用 wait() 阻塞
    等结果——注意等的是「排队 + 出图」全程，排队时间不由我们控制。
    """

    def __init__(self, target, target_id, workflow):
        self.target = target
        self.target_id = target_id
        self.workflow = workflow
        self.prompt_id = None
        self.entry = None           # 出图后的 history entry
        self.error = None           # 失败原因（网页侧 wait 时抛出来）
        self.done = threading.Event()

    def wait(self, poll=POLL_INTERVAL):
        """等到出图。中途用户点「停止」就放弃等待（图仍在后台跑完）。

        每隔 poll 秒醒一次查中断信号——不能一直阻塞在 Event 上，否则用户点
        了停止要等到出图才有反应。
        """
        while not self.done.wait(poll):
            if is_cancelled():
                raise Cancelled("用户中断了等待")
        if self.error is not None:
            raise self.error
        return self.entry


def _key(target, target_id):
    return (str(target or ""), str(target_id or ""))


def inflight_count(target, target_id):
    """这个会话还有几张在途（含排队中）。"""
    with _lock:
        return _per_session.get(_key(target, target_id), 0)


def queue_depth():
    """队列里还有几个在等（含正在跑的那个）。"""
    with _lock:
        return len(_queue) + (1 if _running is not None else 0)


def ahead_of(job):
    """这个任务前面还有几张（含正在跑的那张）。已经开跑就返回 0。"""
    with _lock:
        if job not in _queue:
            return 0
        return _queue.index(job) + (1 if _running is not None else 0)


def enqueue(target, target_id, workflow):
    """把一张图排进全局队列，返回 (job, reason)。

    reason 非 None 表示没接（此时 job 为 None），它是一句可以直接转述给对方
    的话。两种拒收：全局队排太长、这个会话自己排太多。
    """
    key = _key(target, target_id)
    with _lock:
        depth = len(_queue) + (1 if _running is not None else 0)
        if depth >= MAX_QUEUE:
            return None, ("现在排队的人太多了（前面还有 %d 张），这一张先不画。"
                          "别跟对方提这张图，当没画过，接着把话说完。" % depth)
        cur = _per_session.get(key, 0)
        if cur >= MAX_INFLIGHT:
            return None, ("这个会话已经排着 %d 张了，画完这些再说。"
                          "不要跟对方提这张图，当没画过，接着把话说完。" % cur)
        job = Job(target, target_id, workflow)
        _queue.append(job)
        _per_session[key] = cur + 1
    _wake.set()
    _ensure_worker()
    return job, None


def _ensure_worker():
    """确保唯一的 worker 线程在跑。重复调用无副作用。"""
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
    try:
        threading.Thread(target=_worker, daemon=True,
                         name="image-worker").start()
    except Exception:
        with _lock:                 # 线程没起来就别占着「已启动」的位
            _worker_started = False
        raise


def _take_nowait():
    """立刻取一个任务；队列为空返回 None。"""
    global _running
    with _lock:
        if not _queue:
            return None
        job = _queue.popleft()
        _running = job
        return job


def _take():
    """取下一个任务；没有就阻塞等（每秒醒一次，保证收得到新入队信号）。"""
    while True:
        job = _take_nowait()
        if job is not None:
            return job
        _wake.wait(1)
        _wake.clear()


def _worker():
    """唯一的工作线程：一张接一张，串行到底。"""
    while True:
        job = _take()
        try:
            process(job)
        except Exception:
            log.exception("生图任务处理时抛异常 %s %s", job.target, job.target_id)
        finally:
            _finish(job)


def process(job):
    """跑一个任务：提交 → 限时等出图 → 发回原会话 / 存给网页侧。

    独立成函数是为了能同步调用（测试直接调它，不依赖真线程）。
    """
    try:
        job.prompt_id = _queue_prompt(job.workflow)
    except Exception as exc:
        job.error = exc
        _notice(job)
        return

    try:
        entry = wait_done(job.prompt_id, TASK_TIMEOUT)
    except TimeoutError as exc:
        # 关键：中断 + 从队列摘掉 + 释放显存。串行队列下「当前正在执行」的
        # 就是我们这一张，所以 /interrupt 是准的——这也是改成全局串行之后
        # 才敢用它（从前不知道在跑的是不是自己的图，只能干等）。
        _abort(job.prompt_id)
        # /interrupt 是异步的：ComfyUI 要等当前节点跑完这一步才真正停下，
        # 这期间提交的新任务只会被排进它的 pending。在这里等到 running 清空、
        # 显存真的腾出来，才把下一个任务交出去——不靠盲等固定秒数。
        _wait_comfy_idle()
        job.error = exc
        _notice(job)
        return

    names = output_images(entry)
    if not names:
        job.error = RuntimeError("跑完了但没找到图片")
        _notice(job)
        return

    job.entry = entry
    if job.target is None:
        return                      # 网页侧自己从 job.entry 取

    try:
        for name in names:
            _send_image(job.target, job.target_id, name)
        log.info("生图完成已发回 %s %s：%d 张",
                 job.target, job.target_id, len(names))
    except Exception as exc:
        job.error = exc
        _notice(job)


def _finish(job):
    """还名额、清 _running、唤醒等结果的网页侧。失败路径也一定要走到。"""
    global _running
    with _lock:
        key = _key(job.target, job.target_id)
        n = _per_session.get(key, 0) - 1
        if n > 0:
            _per_session[key] = n
        else:
            _per_session.pop(key, None)
        if _running is job:
            _running = None
    job.done.set()


def _drain():
    """把队列里的任务同步跑完，不依赖 worker 线程（测试用）。

    有了它，测试就能像从前那样「调用 → 立刻断言提交了什么」，不必陪真线程
    玩时序。
    """
    while True:
        job = _take_nowait()
        if job is None:
            return
        try:
            process(job)
        finally:
            _finish(job)


def _reset():
    """清空队列与计数（测试用）。不动 _worker_started——测试自己把
    _ensure_worker mock 成空操作，真线程不该被这里牵起来。"""
    global _running
    with _lock:
        _queue.clear()
        _per_session.clear()
        _running = None


# ─── ComfyUI 交互 ────────────────────────────────────

def _queue_prompt(workflow):
    resp = requests.post(COMFYUI_URL + "/prompt", json={
        "prompt": workflow,
        "client_id": "agent_" + str(uuid.uuid4())[:8],
    }, timeout=30)
    resp.raise_for_status()
    return resp.json()["prompt_id"]


def _abort(prompt_id):
    """超时/中断：把任务从 ComfyUI 里摘掉并释放显存。

    三步都尽力而为——ComfyUI 可能已经崩了，那也没得清；清理失败不该再抛错，
    因为调用方接下来还要去发「超时了请重画」那句话。
    """
    # /interrupt 带 prompt_id 做定向中断：只有当前正在跑的就是这张时才会停，
    # 否则 ComfyUI 直接跳过。不带 id 是全局中断，会把别的 worker（web/qq_bot
    # 共用一个 ComfyUI）正在跑的图劈掉——出过这种事故。
    for path, payload in (("/interrupt", {"prompt_id": prompt_id}),
                          ("/queue", {"delete": [prompt_id]}),
                          ("/free", {"unload_models": True,
                                     "free_memory": True})):
        try:
            requests.post(COMFYUI_URL + path, json=payload, timeout=15)
        except Exception:
            log.debug("清理 ComfyUI(%s) 失败，忽略", path)


def _wait_comfy_idle(timeout=90):
    """等 ComfyUI 真正闲下来（queue_running 清空）再返回。

    被中断的任务要等当前节点跑完才退场，早于此提交的新任务只会堆进 ComfyUI
    的 pending——虽然它不会跟旧任务抢显存（装载被排在退场之后），但旧任务的
    退场清理和显存回收就和新任务搅在一起，出了慢图分不清是谁的锅。这里轮询
    到 running 清空、补一次 /free 并记一行资源状态，保证下一张从干净状态起跑。

    ComfyUI 挂了也照常返回（False）——清不了就清不了，不能因为清理把队列卡死。
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(COMFYUI_URL + "/queue", timeout=10)
            resp.raise_for_status()
            q = resp.json()
            if isinstance(q, dict) and not (q.get("queue_running") or []):
                _report_and_free()
                return True
        except Exception:
            log.debug("查 ComfyUI 队列失败，忽略", exc_info=True)
        time.sleep(POLL_INTERVAL)
    log.warning("等 ComfyUI 退场超过 %ds，放弃等待直接继续", timeout)
    return False


def _report_and_free():
    """补一次 /free，并记一行显存/内存——下次再慢，一眼看出是被谁挤爆的。"""
    try:
        requests.post(COMFYUI_URL + "/free",
                      json={"unload_models": True, "free_memory": True},
                      timeout=15)
    except Exception:
        pass
    try:
        resp = requests.get(COMFYUI_URL + "/system_stats", timeout=10)
        stats = resp.json()
        dev = (stats.get("devices") or [{}])[0]
        sysinfo = stats.get("system") or {}
        g = 2 ** 30
        log.info("ComfyUI 已空闲，显存余 %.1fGB（共 %.1fGB），内存余 %.1fGB",
                 dev.get("vram_free", 0) / g, dev.get("vram_total", 0) / g,
                 sysinfo.get("ram_free", 0) / g)
    except Exception:
        pass


def poll_once(prompt_id):
    """问一次 ComfyUI 出图没有；没出或问不到返回 None。

    网络抖动、半截 JSON 一律当「还没好」——轮询本来就是容错手段，一次失败
    不影响大局，真出不了图最后由超时兜住。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/history/" + str(prompt_id),
                            timeout=10)
        resp.raise_for_status()
        history = resp.json()
    except Exception:
        return None
    if isinstance(history, dict) and prompt_id in history:
        return history[prompt_id]
    return None


def wait_done(prompt_id, timeout=None):
    """轮询等到出图，返回 history entry；超时抛 TimeoutError。"""
    limit = timeout if timeout and timeout > 0 else TASK_TIMEOUT
    start = time.time()
    while time.time() - start < limit:
        entry = poll_once(prompt_id)
        if entry is not None:
            return entry
        time.sleep(POLL_INTERVAL)
    raise TimeoutError("生成超时 (" + str(limit) + "s)")


def output_images(entry):
    """从 history entry 里掏出文件名列表；没有返回空列表。"""
    out = []
    for node in (entry.get("outputs") or {}).values():
        if not isinstance(node, dict):
            continue
        for img in (node.get("images") or []):
            if isinstance(img, dict) and img.get("filename"):
                out.append(img["filename"])
    return out


# ─── 回话 ────────────────────────────────────────────

def _reason(exc):
    """失败原因一句话（截断防刷屏）。"""
    if isinstance(exc, TimeoutError):
        return "超时"
    return (str(exc) or type(exc).__name__)[:30]


def _fail_text(exc):
    """给对方看的失败说明。超时单独给一句——它最常见，而且对方重画一次就好
    （重画时模型已经加载在显存里，通常几秒到几十秒就出）。"""
    if isinstance(exc, TimeoutError):
        return ("画超时了（超过 %d 秒没出图），已经中断这张。"
                "麻烦重新生成一次。" % TASK_TIMEOUT)
    return "图没画出来（%s）" % _reason(exc)


def _notice(job):
    """QQ 侧失败了就吭一声——对方点了单，图没了却一声不吭会让人干等。

    网页侧不用：异常会由 job.wait() 抛给调用方，再由工具结果告诉模型。
    """
    log.warning("生图任务失败 %s %s：%s", job.target, job.target_id, job.error)
    if job.target is None:
        return
    try:
        _send_text(job.target, job.target_id, _fail_text(job.error))
    except Exception:
        log.exception("生图失败说明也发不出去 %s %s", job.target, job.target_id)


def _send_text(target, target_id, text):
    from app import qq_api
    if target == "group":
        qq_api.send_group(target_id, text)
    else:
        qq_api.send_private(target_id, text)


def _send_image(target, target_id, filename):
    """发回原会话。先过 image_out 甩掉 PNG 里的工作流元数据，编码格式看管理页开关。"""
    from app import image_out, qq_api
    from app.agents import image_send_format
    fmt = image_send_format(QQ_AGENT_ID, target, target_id)
    qq_api.send_image(target, target_id, image_out.prepare_for_send(filename, fmt))
