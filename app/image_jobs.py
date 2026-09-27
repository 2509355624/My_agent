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
- **/free 在三种时候打**：① 异常路径（超时/失败）——超时往往伴随显存已经
  被啃满，先清干净再让下一张上，免得连锁崩；② **渠道切换**——正常跑完继续
  用同一个模型更快，但**换渠道时这个理由不成立**：旧渠道的模型下一张根本
  用不上，留着纯粹占地方；③ **显存低于水位**（2026-09-27 加，见
  _maybe_release_for_low_vram）——ComfyUI 从不把上一个任务清干净（日志
  `Unloaded partially: … remains loaded`，残留 1.6~2.1GB）。这条只是保险，
  管不了 qwen 的根因（权重 10.5GB vs 空闲可用 10.78GB），详见那个函数的注释。
- **换渠道先 /free**（2026-09-27 加）：12GB 显存 + 16GB 内存撑不住两个渠道的
  模型同时驻留（anima 单阶段约 5.4GB，qwen 约 11.1GB——光文本编码器就 6GB）。
  实测：anima 连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen，采样到一半
  就触发 nvlddmkm 153（TDR），ComfyUI 整个变成僵尸、8188 永久 500。
  同渠道连画不受影响，模型还是热的。
- **会话身份在入队那一刻快照**：qq_api 的上下文是线程本地的，worker 线程取
  不到，所以 target / target_id 必须随任务带过去（猜错就发错群）。
- **网页侧同步等，但等的时候不占 worker**：worker 只管跑图，网页请求线程自
  己在 Job.wait 上阻塞，两者分开。
- **重渠道排到最后，且几乎不让它连跑两张**（2026-09-27 加，见下面的「重渠道
  优先度」一节）：qwen_image_v1 权重 5，任何普通渠道（权重 1）都能插到它前面；
  前一张 qwen 刚跑完 90 秒内，qwen 一律重新排队尾，把间隙让给别的渠道——那个
  间隙正是 TDR 的窗口。**这是软的**：真没有别的活时 qwen 照跑，不会饿死。
- **重渠道不许排长队**：排到第三张就拒收（`_MAX_HEAVY_QUEUE`）。qwen 一张约
  100 秒，3 张已经把「重活独占显卡」的时间撑到 5 分钟，再多不如让模型说画不了。
- **要「干净 ComfyUI」的渠道开跑前先重启一次**（2026-09-27 加，见
  _maybe_restart_for_clean_start）：anima_2（双底模两段）要把两张底模各 3988MB
  + 文本编码器 1136MB + VAE 241MB ≈ 9.4GB 同时摊开，而上一张跑完（哪怕打过
  /free）显存只剩 5.6GB——硬提交就是 180 秒超时。刚起来的 ComfyUI 有 10.8GB，
  够。**事前**重启一次（约 60~90 秒，模型要重新加载）比事后超时划算；跟
  _maybe_restart_for_ram 那条不冲突：那条是事后补、这条是事前拦。
- **内存水位过低就重启 ComfyUI**（2026-09-27 加，见 _maybe_restart_for_ram）：
  `/free` 治不了内存——它只把权重从显存搬到 CPU，**不删**，进程 RSS 一个字节
  都不降（实测 8025 → 8025 MB）。而 ComfyUI 的常驻内存每张图涨约 600MB、只涨
  不落，挤干物理内存后 GGUF 要从磁盘重读、采样卡死。重启是唯一能把内存真正
  还回去的手段，代价是下一张要重新加载模型（十几秒到一分钟）。
  **这跟「换渠道先 /free」不冲突**：那条管显存，这条管内存。

## 重渠道优先度（2026-09-27 加）

### 为什么

qwen_image_v1 是唯一「一跑就把 12GB 显卡榨干」的渠道：文本编码器 6018MB +
unet 4487MB ≈ 10.5GB 权重，而空闲可用只有 10.78GB。当天实测的错误模式是
**确定性的**：

- **第 1 张必成**（48~50 秒）；
- **第 2 张必死**——提交后 2~6 秒，日志停在 `got prompt` 那行中间，没有 Python
  traceback，紧接着 `nvlddmkm` 事件 153（TDR），有时整台机器直接重启。

死因是「第 1 张的残留在 VRAM / RAM 里还没散，第 2 张就要重新摊开那 6000MB 的
编码器」——出图后显存只剩 2.35GB、内存只剩 4.21GB，两边都装不下。早先「16:09
成 / 16:12 成」那两次能连上，是因为中间隔了三分钟，缓存自己过期了。

外挂的启动参数全试过、全被推翻（`--vram-headroom` 反而拖垮了整机、
`--disable-pinned-memory` 第一张就撞出完整 TDR、`--lowvram` 在 DynamicVRAM 下是
空操作），量化也已经到底（Q4_K_M + w4a8 就是 8GB 显存档的官方最省组合，再往下
只有 Q4_0，还要掉画质）。**所以只能从 agent 侧管**：别让 qwen 紧接着 qwen 跑。

### 两条规则

1. **权重拉开**：`skill_priority()` 给每个渠道定权重（默认 1；qwen 是 5）。
   队列按 `(是否非默认, 权重, seq)` 排序，所以任何普通渠道都能插到任何重渠道
   前面，**不管它是什么时候入队的**。这就是用户要的「有其他渠道生成的时候 qwen
   必须最后自动顺位到后面」。
2. **冷却窗**：一张 qwen 跑完之后的 `QWEN_COOLDOWN`（默认 90）秒内，新的 qwen
   重新扔到队尾，并且按普通优先级参与排序——把让出来的空隙给别的渠道。冷却窗
   本来正是显存/内存把 6GB 权重还回去所花的时间。

### 有意不做的

- **不做「单列 qwen 通道」**：worker 是全局唯一的，第二条通道只会让两张图并行
  ——而并行正是要根除的东西。
- **不饿死 qwen**：上面两条都是「软」的——排序只在有别的活可干时把 qwen 往后放，
  队列里只剩 qwen 时它照跑。
- **不额外改 qwen 的分辨率 / cache 设置**：那会掉细节，且没有实测支撑。本来就有
  `_maybe_release_for_low_vram` 在提交前兜一道 `/free`。
"""

import collections
import logging
import threading
import time
import uuid

import requests

from app.cancel import Cancelled, is_cancelled
from app.config import (COMFY_MIN_FREE_RAM_GB, COMFY_MIN_FREE_VRAM_GB,
                        COMFY_RESTART_MIN_GAP, COMFY_RESTART_WAIT, COMFYUI_URL,
                        IMAGE_GEN_TIMEOUT, QQ_AGENT_ID, QWEN_COOLDOWN)
from app.skills import skill_priority

log = logging.getLogger("image_jobs")

# 同一会话同时在途（含排队中）的张数上限：防一个人连环点单把队列占满。
MAX_INFLIGHT = 2

# 全局队列上限（含正在跑的那张）。十几个群同时刷图时，不能让队列无限长——
# 排到一小时之后的图，对方早就不看了。超了就让模型回一句「排队的人太多」。
MAX_QUEUE = 10

# 重渠道（权重 > 1）在队里最多允许多少张（含正在跑的那张）。qwen 一张约
# 100 秒，3 张就把「重活独占显卡」的时间撑到 5 分钟——再多不如让模型说画不了。
# 权重本身说明不了队有多长，所以这条单独数。
MAX_HEAVY_IN_QUEUE = 3

# 单张图从「真正开跑」到出图的时限（秒）。到点还没出图就中断它、让下一个上。
TASK_TIMEOUT = IMAGE_GEN_TIMEOUT

# 轮询间隔（秒）
POLL_INTERVAL = 2

# 冷却闸只认这一个渠道（见模块开头「重渠道优先度」）。写成常量而不是判断
# 「权重 > 1」，是因为冷却的必要性来自 qwen 那套权重的具体尺寸，别的重渠道
# 不一定共用同一个死因。
QWEN_SKILL = "qwen_image_v1"

# 开跑前必须先要一个「干净 ComfyUI」的渠道：值 = 至少要有的空闲显存（GB）。
#
# anima_2 为什么在里面（2026-09-27 实测）：双底模两段要把两张底模各 3988MB +
# 文本编码器 1136MB + VAE 241MB ≈ 9.4GB 同时摊开。而上一张 anima 跑完（打过
# /free 也一样，它只把权重搬到 CPU）显存只剩 5.6GB，硬提交就是 180 秒超时
# ——19:35 的日志：渠道切换 anima → anima_2、显存余 5.6GB，接着「生成超时」。
# ComfyUI 刚起来时是 10.8GB，够。所以阈值定在两者之间：8.0GB。
#
# ⚠️ 这只能把「必超时」变回「跑得完」，**不保证不崩机**：anima_2 就是那个两段
# 式，13:50 / 13:53 / 13:57 三次 TDR 都是它。显存够了不等于安全，它保持
# 「只在对方点名时才用」的定位。
CLEAN_START_SKILLS = {"anima_2": 8.0}

_lock = threading.Lock()
_queue = collections.deque()      # 待跑的任务（不含正在跑的那个）
_running = None                   # 正在跑的任务（只为可观测 / 算排队位次）
_last_skill = None                # 上次提交给 ComfyUI 的渠道，用来判断要不要先 /free
_last_restart_try = 0.0           # 上次**尝试**重启 ComfyUI 的时刻，防抖（成败都记）
_per_session = {}                 # (target, target_id) -> 在途张数（含排队）
_worker_started = False
_wake = threading.Event()         # 有新任务入队时戳一下 worker

# 排序用。_seq 单调递增，权重相同的任务严格先进先出；_heavy_done_at 记
# 上一张重渠道**跑完**的时刻（不是提交时刻——冷却要的是「残留在散」的那段）。
_seq = 0
_heavy_done_at = 0.0


def _order(job):
    """队列排序键：先普通渠道（按入队先后），再重渠道（按入队先后）。

    排的是「出队顺序」，在**取出时**才排序（见 _take_nowait），所以入队顺序
    不丢——后入队的普通任务能插到先入队的 qwen 前面，这正是用户要的
    「有其他渠道生成的时候 qwen 必须最后自动顺位到后面」。

    三个字段：① 权重是不是默认值（False=普通，排前面）；② 权重（都非默认时
    小的先）；③ 入队序号。故意不用 `job.weight != 1` 而用 `job.weight > 1`：
    万一以后出现权重 0（更优先），它也该排在最前面，而不是被当成「非默认」。
    """
    return (job.weight > 1, job.weight, job.seq)


def _heavy_ahead():
    """已经在等或正在跑的重渠道有几张（含正在跑的那张）。"""
    return sum(1 for j in _queue if j.weight > 1) \
        + (1 if _running is not None and _running.weight > 1 else 0)


class Job:
    """一次生图任务。

    QQ 侧提交完即返回（图由 worker 直接发回原会话）；网页侧用 wait() 阻塞
    等结果——注意等的是「排队 + 出图」全程，排队时间不由我们控制。
    """

    def __init__(self, target, target_id, workflow, skill=None, weight=1, seq=0):
        self.target = target
        self.target_id = target_id
        self.workflow = workflow
        self.skill = skill          # 生图渠道，只用来判断要不要先 /free
        self.weight = weight        # 队列权重（默认 1 = 普通；qwen 是 5）
        self.seq = seq              # 入队序号，同权重的按它先进先出
        self.waits = 0              # 被冷却 / 被插队推回过几次（只为日志）
        self.skill_done = False     # 已经成功跑完一张？决定跑完要不要开冷却
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
    """这个任务前面还有几张（含正在跑的那张）。已经开跑就返回 0。

    按**出队顺序**数，不是按入队顺序：后入队的普通任务会插到先入队的 qwen
    前面，模型报给对方的「前面还有 N 张」得跟它实际要等的一致。
    """
    with _lock:
        if job not in _queue:
            return 0
        order = sorted(_queue, key=_order)
        return order.index(job) + (1 if _running is not None else 0)


def enqueue(target, target_id, workflow, skill=None):
    """把一张图排进全局队列，返回 (job, reason)。

    reason 非 None 表示没接（此时 job 为 None），它是一句可以直接转述给对方
    的话。三种拒收：全局队排太长、这个会话自己排太多、重渠道已经排了太多。

    skill 用来定两件事：队列权重（`skill_priority`，qwen 排最后）和
    「换渠道先 /free」的判断（见 _maybe_release_for_switch）。
    不传 skill 时的行为与从前完全一致（权重 1、从不主动释放）——老调用方
    不受影响。
    """
    global _seq
    key = _key(target, target_id)
    weight = skill_priority(skill)
    with _lock:
        depth = len(_queue) + (1 if _running is not None else 0)
        if depth >= MAX_QUEUE:
            return None, ("现在排队的人太多了（前面还有 %d 张），这一张先不画。"
                          "别跟对方提这张图，当没画过，接着把话说完。" % depth)
        cur = _per_session.get(key, 0)
        if cur >= MAX_INFLIGHT:
            return None, ("这个会话已经排着 %d 张了，画完这些再说。"
                          "不要跟对方提这张图，当没画过，接着把话说完。" % cur)
        if weight > 1 and _heavy_ahead() >= MAX_HEAVY_IN_QUEUE:
            # 重渠道一张就把显卡占满好几分钟。排队太长不如让它说一句
            # 「我现在画不了」——总好过半小时后发一张对方早忘了的图。
            return None, ("画图那个通道正忙着（已经在排 %d 张重的），这一张先不画。"
                          "别跟对方提这张图，当没画过，接着把话说完。"
                          % MAX_HEAVY_IN_QUEUE)
        _seq += 1
        job = Job(target, target_id, workflow, skill, weight, _seq)
        _queue.append(job)
        _per_session[key] = cur + 1
    if weight > 1:
        # 入队就记一行：模型选渠道的决策只有在这里才看得见，出问题时先查这行。
        log.info("重渠道 %s 入队（%s %s，权重 %d，前面 %d 张）",
                 skill, target, target_id, weight, ahead_of(job))
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


def _next_ready():
    """挑下一个该跑的任务（**调用方必须已持 _lock**）；都不该跑返回 None。

    「重渠道不许连跑」在这里落地：只要**上一张成功的是重渠道**且还没过冷却窗，
    队里的重渠道就一张都不许开跑——普通渠道照常放行（这正是冷却窗的意义：
    把间隙让给别人）。全是重渠道、又都在冷却里时返回 None，worker 回去等。

    两个容易踩的点：

    - 判据读的是 `_heavy_done_at`（跑**完**的时刻）而不是提交时刻：要防的是
      「前一张卸载下来的那几秒正好撞上后一张的模型装载」，从提交起算会把窗口
      整个错开。
    - 冷却窗一过就**立刻**把队里的重渠道重新排好（按 _order 取最小），所以
      冷却结束不需要任何额外的唤醒信号——worker 每秒醒一次，下一轮就看见了。
      这也意味着「冷却中的重渠道不占用 ahead_of 的名额」是自动成立的。
    """
    if not _queue:
        return None
    if _cooling():
        ready = [j for j in _queue if j.weight <= 1]
        if not ready:
            return None
        return min(ready, key=_order)
    return min(_queue, key=_order)


def _cooling():
    """重渠道现在在冷却里吗（排队阶段的闸）；调用方已持 _lock。"""
    if QWEN_COOLDOWN <= 0 or _heavy_done_at <= 0:
        return False
    return (time.time() - _heavy_done_at) < QWEN_COOLDOWN


def _move_back(job):
    """把一个任务挪到队尾（冷却没到点的重渠道用）；调用方已持 _lock。

    不是 `job.seq = _seq+1` 了事——那样所有冷却里的重渠道会**共享同一个序号**，
    重排时的先后就变成集合顺序（不确定）。真挪到队尾：重新取号并移到 deque
    尾部，让「谁先被推回去谁先出来」稳定下来。
    """
    global _seq
    try:
        _queue.remove(job)
    except ValueError:
        return                          # 已经被别的路径取走了
    _seq += 1
    job.seq = _seq
    job.waits += 1
    _queue.append(job)
    log.info("重渠道 %s 让行（第 %d 次）：上一张 %s 刚跑完不到 %.0f 秒，"
             "先让普通渠道上", job.skill, job.waits, QWEN_SKILL, QWEN_COOLDOWN)


def _take_nowait():
    """立刻取一个任务；没有 / 都还在冷却里就返回 None。"""
    global _running
    with _lock:
        if not _queue:
            return None
        # 上一张要是重渠道，现在又还在冷却里，先把它挪到队尾——
        # 否则它会是 _order 的最小项，直接被取走，让行规则形同虚设。
        if _cooling():
            for job in [j for j in _queue if j.weight > 1]:
                _move_back(job)
        job = _next_ready()
        if job is None:
            return None
        try:
            _queue.remove(job)
        except ValueError:
            return None
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
        # 每跑完一张看一眼内存——ComfyUI 的常驻内存是按张涨的（见
        # _maybe_restart_for_ram）。放在这里而不是 _take 之前：_take 会阻塞
        # 等新任务，在那儿检查就变成每秒一次了。
        _maybe_restart_for_ram()


def _maybe_release_for_switch(job):
    """渠道变了就先让 ComfyUI 把上一个渠道的模型卸掉。

    为什么不能只靠异常路径那条规则（见模块开头「关键取舍」）：那条规则的理由
    是「正常跑完继续用同一个模型更快」——**换渠道时这个理由不成立**，旧渠道的
    模型下一张根本用不上，留着只是把显存和内存占住。2026-09-27 实测：anima
    单阶段连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen（文本编码器
    6GB + unet 4.5GB），采样到一半就 TDR，ComfyUI 直接变成僵尸。

    skill 为 None（老调用方没传）时整个函数是空操作，行为与从前完全一致；
    同渠道连画也不打 /free，模型保持热的。
    """
    global _last_skill
    skill = job.skill
    if skill is None:
        return
    prev = _last_skill
    _last_skill = skill             # 无论打不打 /free 都要记，否则会反复触发
    if prev is None or prev == skill:
        return
    log.info("渠道切换 %s → %s，先释放上一个渠道的模型", prev, skill)
    _report_and_free()


def _free_ram_gb():
    """问 ComfyUI 系统还剩多少可用内存（GB）；问不到返回 None。

    借 ComfyUI 自己报的数，省一个 psutil 依赖——它报的 `system.ram_free` 与
    psutil 的 `virtual_memory().available` 语义一致（实测 1.61 vs 1.60 GB）。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/system_stats", timeout=10)
        resp.raise_for_status()
        return (resp.json().get("system") or {}).get("ram_free", 0) / 2 ** 30
    except Exception:
        log.debug("查 ComfyUI 内存失败，忽略", exc_info=True)
        return None


def _free_vram_gb():
    """问 ComfyUI 显存还剩多少（GB）；问不到返回 None。

    与 _free_ram_gb 同一套路：借 ComfyUI 自己报的数，不引 psutil。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/system_stats", timeout=10)
        resp.raise_for_status()
        devs = resp.json().get("devices") or [{}]
        return devs[0].get("vram_free", 0) / 2 ** 30
    except Exception:
        log.debug("查 ComfyUI 显存失败，忽略", exc_info=True)
        return None


def _maybe_release_for_low_vram():
    """显存快见底就先 /free，把上一张的残留腾出来再提交。

    **为什么需要这条**（2026-09-27 加）：_maybe_release_for_switch 只在**换
    渠道**时释放，同渠道连画不释放。而 ComfyUI **从不把上一个任务清干净**
    ——日志里那句 `Unloaded partially: 2896.25 MB freed, 1591.04 MB remains
    loaded` 就是证据，残留 1.6~2.1GB 会一路叠上去。实测 qwen 连画
    16:09 成 / 16:12 成 / 16:14 崩，看着就是残留累积。

    阈值（默认 5.0GB）是量出来的：anima 跑完还剩约 5.5GB，不该动它；qwen
    跑完只剩约 0.8GB，下一张必须先清。所以正常连画 anima 不受影响。

    ⚠️ **但它不是 qwen 崩溃的解药**（16:22 真机实测推翻）：ComfyUI 刚重启、
    显存全空 10.78GB、第一张 qwen 照样崩。真正的天花板是权重本身——
    TE 6018MB + unet 4487MB = 10.5GB，而空闲可用只有 10.78GB（约 1.16GB
    被桌面占着），只剩约 0.5GB 给激活值。这条水位只是「别让残留把本就紧张的
    空间再吃掉一块」，是保险不是解药。
    """
    if COMFY_MIN_FREE_VRAM_GB <= 0:
        return                       # 功能关掉了
    free = _free_vram_gb()
    if free is None or free >= COMFY_MIN_FREE_VRAM_GB:
        return
    log.info("显存只剩 %.1fGB（低于 %.1fGB 水位），先 /free 再提交",
             free, COMFY_MIN_FREE_VRAM_GB)
    _report_and_free()


def _restart_comfy(timeout=COMFY_RESTART_WAIT):
    """重启 ComfyUI 进程并等它回来；成功返回 True。

    走 ComfyUI-Manager 的 `/manager/reboot`：Legacy 模式（进程里没有
    `__COMFY_CLI_SESSION__`）下它是 `os.execv` 原地重启、**保留原命令行**，
    所以不用我们管进程怎么起。要求 Manager 的 `security_level` 不高于
    normal（在 `user/__manager/config.ini` 里），否则返回 403。

    **它不会返回 200**：ComfyUI 是先 `exit(0)` 再回包的，所以客户端拿到的是
    连接被强行关闭（实测 `ConnectionResetError` 10054）。那不是失败——恰恰
    说明它真的在重启，所以这里把它当「已发出，去等它回来」处理。

    重启期间 8188 会拒连，所以轮询到它回来为止。等不到就放弃并记一条错误
    ——下一张图会撞上 `_notice` 那句「ComfyUI 没在线」，总好过在这里无限等。
    """
    try:
        status = requests.get(COMFYUI_URL + "/manager/reboot",
                              timeout=15).status_code
    except Exception as exc:
        log.info("重启请求以 %s 结束——ComfyUI 先退出再回包，属正常",
                 type(exc).__name__)
        status = None                # 不是拒绝，继续往下等它回来
    if status is not None and status != 200:
        log.warning("重启 ComfyUI 被拒（HTTP %d）——多半是 ComfyUI-Manager 的 "
                    "security_level 高于 normal，见 user/__manager/config.ini",
                    status)
        return False

    log.info("ComfyUI 正在重启，等它回来（最多 %.0f 秒）", timeout)
    time.sleep(3)                    # 先等旧进程真的交出去，别刚发完就探到它
    start = time.time()
    while time.time() - start < timeout:
        try:
            if requests.get(COMFYUI_URL + "/system_stats",
                            timeout=5).status_code == 200:
                log.info("ComfyUI 已重启完成，耗时 %.0f 秒", time.time() - start)
                # 新进程里一个模型都没加载，别让「换渠道先 /free」以为还是热的。
                global _last_skill
                with _lock:
                    _last_skill = None
                return True
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)
    log.error("等 ComfyUI 重启超过 %.0f 秒还没回来", timeout)
    return False


def _maybe_restart_for_ram():
    """可用内存过低就重启 ComfyUI，把它占的内存真正还回去。

    **为什么必须重启、而不是打 /free**：`/free` 只做
    `model.to(offload_device)`——把权重从显存搬到 CPU，**不删**。所以进程
    RSS 一个字节都不降（2026-09-27 实测 8025 → 8025 MB），而它的常驻内存
    每张图涨约 600MB、只涨不落。挤干物理内存之后的症状是「卡」不是「崩」：
    GGUF 每次从磁盘重读（5.5 秒 → 68 秒）、采样卡在 0/N 一百秒，整机跟着
    换页。重启是唯一有效的止血。

    调用点在 worker 里、**每跑完一张检查一次**——内存是按张涨的，所以按张
    看。队列空的时候重启最划算（没人在等）；队列不空也得重启，否则下一张
    照样卡死，后面排队的全陪葬。

    代价：重启会丢掉 ComfyUI 里已加载的模型，下一张要重新加载（十几秒到
    一分钟）。这是拿时间换「不卡死」。
    """
    global _last_restart_try
    if COMFY_MIN_FREE_RAM_GB <= 0:
        return                       # 功能关掉了
    now = time.time()
    if now - _last_restart_try < COMFY_RESTART_MIN_GAP:
        return                       # 刚试过，别反复折腾（也防失败后每张刷日志）
    free = _free_ram_gb()
    if free is None or free >= COMFY_MIN_FREE_RAM_GB:
        return
    # 成败都记：不记的话重启被拒时会每张图重试一次，日志刷屏还白等 3 秒。
    _last_restart_try = now
    log.warning("系统可用内存只剩 %.1fGB（低于 %.1fGB 水位），重启 ComfyUI 释放",
                free, COMFY_MIN_FREE_RAM_GB)
    _restart_comfy()


def _maybe_restart_for_clean_start(job):
    """这个渠道要「干净的 ComfyUI」才跑得动：显存不够就先重启一次。

    跟 _maybe_restart_for_ram 的区别：那条是**事后**（跑完一张发现内存被啃低
    了才补），这条是**事前**（明知道这个渠道要 9.4GB，先看够不够再说）。只有
    事前才拦得住——事后重启的时候那张图已经超时失败了。

    两个「不折腾」的早退：显存**问不到**（None）时什么都不做——那种情况下
    ComfyUI 多半已经不在了，重启请求同样发不出去，还不如照常提交，让 _notice
    去说一句「ComfyUI 没在线」，比在这里白等 180 秒诚实。

    每张图最多重启一次（只在提交前判一次），所以不会退化成重启循环——即使
    重启完显存还是不够，也只是这一张照常提交、照常可能超时。
    """
    need = CLEAN_START_SKILLS.get(job.skill or "") or 0
    if need <= 0:
        return
    free = _free_vram_gb()
    if free is None or free >= need:
        return
    log.info("%s 开跑前要 %.1fGB 显存，现在只剩 %.1fGB——先重启 ComfyUI "
             "要一个干净状态再跑", job.skill, need, free)
    _restart_comfy()


def process(job):
    """跑一个任务：提交 → 限时等出图 → 发回原会话 / 存给网页侧。

    独立成函数是为了能同步调用（测试直接调它，不依赖真线程）。
    """
    # 放在换渠道 /free 之前：重启成功后 _last_skill 会被清成 None（新进程里
    # 一个模型都没加载），换渠道那条就不会再打一次没用的 /free。
    _maybe_restart_for_clean_start(job)
    _maybe_release_for_switch(job)
    _maybe_release_for_low_vram()
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
    job.skill_done = True
    if job.target is None:
        return                      # 网页侧自己从 job.entry 取

    try:
        for name in names:
            _send_image(job.target, job.target_id, name)
        log.info("生图完成已发回 %s %s：%d 张",
                 job.target, job.target_id, len(names))
    except Exception as exc:
        job.error = exc
        _notice(job, stage="send")


def _finish(job):
    """还名额、清 _running、唤醒等结果的网页侧。失败路径也一定要走到。"""
    global _running, _heavy_done_at
    with _lock:
        key = _key(job.target, job.target_id)
        n = _per_session.get(key, 0) - 1
        if n > 0:
            _per_session[key] = n
        else:
            _per_session.pop(key, None)
        if _running is job:
            _running = None
        # 只有**真出图了**的重渠道才开冷却窗。失败/超时那张已经把 ComfyUI 的
        # 队列和显存清干净了（_abort + _wait_comfy_idle），没有残留要等它散，
        # 再罚它 90 秒只是白等。
        if job.weight > 1 and job.skill_done:
            _heavy_done_at = time.time()
            log.info("重渠道 %s 跑完，%.0f 秒内不再接重活",
                     job.skill, QWEN_COOLDOWN)
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
    global _running, _last_skill, _last_restart_try, _seq, _heavy_done_at
    with _lock:
        _queue.clear()
        _per_session.clear()
        _running = None
        _last_skill = None
        _last_restart_try = 0.0
        _seq = 0
        _heavy_done_at = 0.0


# ─── ComfyUI 交互 ────────────────────────────────────

def comfy_alive(timeout=3):
    """ComfyUI 现在活着吗——入队前先问一句。

    队列在 agent 侧，enqueue 成功**不代表** ComfyUI 在：它挂了照样收单，
    模型被告知「已经排上队了」，几十秒后 worker 才在 /prompt 上撞到连接
    失败。群里于是先看到一句凭空承诺、再看到一句「图没画出来」，前后打架。

    探活只挡得住最常见的「ComfyUI 根本没开」；探活过了之后再挂，仍由
    _notice 兜底——那是「提交完即返回」这个设计自带的，除非把 /prompt
    挪回调用线程去同步等。
    """
    try:
        requests.get(COMFYUI_URL + "/system_stats",
                     timeout=timeout).raise_for_status()
        return True
    except Exception as exc:
        log.warning("ComfyUI 探活失败（%s）：%s，本次不入队",
                    COMFYUI_URL, type(exc).__name__)
        return False


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

def _is_unreachable(exc):
    """这个失败是「连不上 ComfyUI」吗？

    requests 的 ConnectionError 继承 OSError，str() 出来是一长串
    「HTTPConnectionPool(host='127.0.0.1', port=8188)…」，被 _reason 截 30
    字就只剩半截乱码丢进群里。这类失败原因单一，值得单独说人话。

    两个例外不算：wait_done 的超时另有说法（是画得慢，不是连不上）；带
    response 的异常说明 ComfyUI 活着、是它把工作流拒了（requests 的
    RequestException 一律有 response 属性，只有拿到回应才非 None——所以
    这里认 response 而不认异常类型，也省得依赖 requests 模块对象）。
    """
    if isinstance(exc, TimeoutError):
        return False
    if getattr(exc, "response", None) is not None:
        return False
    return isinstance(exc, OSError)


def _reason(exc):
    """失败原因一句话（截断防刷屏）。"""
    if isinstance(exc, TimeoutError):
        return "超时"
    return (str(exc) or type(exc).__name__)[:30]


def _fail_text(exc, stage="submit"):
    """给对方看的失败说明。

    超时单独给一句——它最常见，而且对方重画一次就好（重画时模型已经加载在
    显存里，通常几秒到几十秒就出）。连不上 ComfyUI 也只给一句人话，不把
    半截 HTTPConnectionPool 甩出去。

    stage="send" 是投递阶段挂的（图都画好了，是发回会话那步失败），跟
    ComfyUI 在不在没关系，别往它头上安。
    """
    if isinstance(exc, TimeoutError):
        return ("画超时了（超过 %d 秒没出图），已经中断这张。"
                "麻烦重新生成一次。" % TASK_TIMEOUT)
    if stage != "send" and _is_unreachable(exc):
        return ("图没画出来——ComfyUI 没在线（%s 连不上）。"
                "让对方稍后再试。" % COMFYUI_URL)
    return "图没画出来（%s）" % _reason(exc)


def _notice(job, stage="submit"):
    """QQ 侧失败了就吭一声——对方点了单，图没了却一声不吭会让人干等。

    网页侧不用：异常会由 job.wait() 抛给调用方，再由工具结果告诉模型。
    """
    log.warning("生图任务失败 %s %s：%s", job.target, job.target_id, job.error)
    if job.target is None:
        return
    try:
        _send_text(job.target, job.target_id, _fail_text(job.error, stage))
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
