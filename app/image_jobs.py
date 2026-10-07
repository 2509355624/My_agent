"""生图任务队列：全局单通道串行，一次只让 ComfyUI 跑一张。

## 它解决什么

原先 submit() 的前提是「调用方已经 _queue_prompt 提交给 ComfyUI 了，这里只
登记一下」。也就是说**排队发生在 ComfyUI 内部**，而 agent 侧既看不见也管不着：

- **显存**：当年 MAX_INFLIGHT 是「每个会话 2 张」，N 个会话并发就是 N×2 张灌进
  ComfyUI 队列。qwen_image_v1 单张峰值就要十几 GB（Q4_K 的 unet + 8B 文本
  编码器），塞进 12GB 显存全靠 offload 硬撑；连堆两张就会崩在
  ComfyUI-GGUF 的 Q4_K 反量化上，而且**会把整个 ComfyUI 一起带崩**。
  （⚠️ 这是**改造前**的病因记录，用来解释这个模块为什么存在。改成 agent 侧
  串行队列之后这条已经不复存在，MAX_INFLIGHT 也早不是显存闸了——见它的定义。）
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
- **重渠道那套已关闭**（2026-09-27 加、**2026-10-04 用户拍板关掉**）：原本
  qwen_image_v1 权重 5（任何普通渠道都能插到它前面）+ 跑完隔 90 秒才许再跑，
  防的是 qwen 连跑第二张 TDR。用户连着跑漫画加字时被这两条卡成「一张等
  2.5~3 分钟」，拍板「我现在没有任何的重渠道」。现在 `skills._SKILL_PRIORITY`
  已清空、`QWEN_COOLDOWN=0`：qwen 与普通渠道一样按**入队顺序**排，
  `MAX_HEAVY_IN_QUEUE`（第 4 张拒收）也随之失效。**代价是连跑风险自担**——
  崩了按 `config.py` 的 `QWEN_COOLDOWN` 注释改回三处即可。详见下面的
  「重渠道优先度」一节（保留了当初的设计理由与一个已知坑）。
- **「开跑前先要一个干净 ComfyUI」这套机制留着，但没有渠道在用了**
  （2026-09-27 加、2026-09-30 清空，见 _maybe_restart_for_clean_start）：本意是给
  「峰值显存逼近脏状态余量」的渠道预备一次重启。实践下来唯一挂进去的 anima_2
  是**误判**——09-30 实测两段采样在脏进程上连跑 8 次全成（见 CLEAN_START_SKILLS
  注释），那个门槛只在一天里白重启了 23 次 ComfyUI。机制本身留着（改 `{}` 即可
  重新启用），但没有渠道需要它。跟 _maybe_restart_for_ram 那条不冲突：那条是
  事后按**内存**水位补，这条是事前按**显存**水位拦。
- **内存水位过低就重启 ComfyUI**（2026-09-27 加，见 _maybe_restart_for_ram）：
  `/free` 治不了内存——它只把权重从显存搬到 CPU，**不删**，进程 RSS 一个字节
  都不降（实测 8025 → 8025 MB）。而 ComfyUI 的常驻内存每张图涨约 600MB、只涨
  不落，挤干物理内存后 GGUF 要从磁盘重读、采样卡死。重启是唯一能把内存真正
  还回去的手段，代价是下一张要重新加载模型（十几秒到一分钟）。
  **这跟「换渠道先 /free」不冲突**：那条管显存，这条管内存。

## 重渠道优先度（2026-09-27 加；**2026-10-04 已按用户要求关闭**）

> ⚠️ **当前不生效**：`skills._SKILL_PRIORITY` 已清空、`QWEN_COOLDOWN=0`，所以
> `_order` 退化成纯先进先出、`_cooling` 恒 False、`_move_back` 与
> `MAX_HEAVY_IN_QUEUE` 都不会被触发。下面这段是**当初为什么这么设计**的记录，
> 连同恢复路径一起留着——qwen 连跑第二张必崩是实测出来的，哪天崩了就按
> `config.py` 里 `QWEN_COOLDOWN` 的注释把三处一起改回来。

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

### 已知坑（2026-10-04 发现，重开冷却前必修）

`_move_back` 是 `_take_nowait` 调用的，而 `_take_nowait` 是 worker **每秒轮询
一次**的入口（`_take` 里 `wake.wait(1)`）。于是冷却窗内每秒都会把队里每张重
渠道重新挪到队尾一次：`job.waits += 1` 且打一条 INFO 日志。后果：

- 90 秒的冷却窗刷出 90~180 条「让行」日志（2026-10-04 实测当天累计 1519 条）；
- 队里有 N 张重渠道时每秒 N 条，日志里会出现**编号相同的多行**（每张各挪一次，
  看起来像同一行重复打印）；
- `job.waits` 从此不是「被让行几次」而是「被轮询了几秒」，完全失真。

正确做法是「状态变化时挪一次」（首次撞上冷却窗时标记，之后不再重复挪）。冷却
窗既然已关（不触发），暂不动它。

### 有意不做的

- **不做「单列 qwen 通道」**：worker 是全局唯一的，第二条通道只会让两张图并行
  ——而并行正是要根除的东西。
- **不饿死 qwen**：上面两条都是「软」的——排序只在有别的活可干时把 qwen 往后放，
  队列里只剩 qwen 时它照跑。
- **不额外改 qwen 的分辨率 / cache 设置**：那会掉细节，且没有实测支撑。本来就有
  `_maybe_release_for_low_vram` 在提交前兜一道 `/free`。

## 两条通道（2026-10-03 用户提「NAI 是云端的，来我本地队列排什么队」）

队列从「一条」变成「两条」，划分标准是**占不占本机显卡**：

- **本地通道**（`_COMFY`）：ComfyUI 那一堆渠道，单 worker 串行。上面写的权重
  排序、冷却窗、换渠道 /free、内存水位重启**全归它**——那些机制的存在理由都是
  显卡（`chan.local` 一票否决云端通道用它们）。
- **NAI 通道**（`_NAI`）：`nai` / `nai_wide` 走 NovelAI 云端，本机只落盘、审核、
  发消息，一帧渲染都不占。所以它**并行**跑（`NAI_CONCURRENCY` 个 worker），
  不再排在本地图后面——拆之前 NAI 点完要等前面十几张本地图，最多十分钟。

两条通道各有一套 queue / running / 每会话计数 / 唤醒信号 / 入队序号，共用同一把
`_lock`（临界区都只有几行、没有磁盘 IO，为并行再拆一套锁不划算）。跨通道的查询
（查重、回执、每会话在途、`recent_activity`）自己遍历两条。

有意**不做同会话保序**（用户选的）：两条队列并行，同一会话的 NAI 图可能比本地图
先发出。要保序就得跨队列同步——一张 NAI 得等本地队前面那张出来，正好把这条通道
的意义抵消掉。

名额是**分通道各算**的：本地 20/5，NAI 20/5（`NAI_*` 常量，同值但独立定义，
将来想单独收紧不用碰本地）。所以理论上两条队各能排 20 张。
"""

import collections
import logging
import random
import re
import threading
import time
import uuid

import requests

from app import image_log
from app.cancel import Cancelled, is_cancelled
from app.config import (COMFY_MIN_FREE_RAM_GB, COMFY_MIN_FREE_VRAM_GB,
                        COMFY_RELEASE_AFTER_SAME, COMFY_RESTART_MIN_GAP,
                        COMFY_RESTART_WAIT, COMFYUI_URL, IMAGE_GEN_TIMEOUT,
                        QQ_AGENT_ID, QWEN_COOLDOWN)
from app.skills import skill_priority

log = logging.getLogger("image_jobs")

# 同一会话同时在途（含排队中）的张数上限：防一个人连环点单把队列占满。
#
# ⚠️ 它**不是显存闸**（2026-10-03 从 2 改成 5 时确认过）：队列早已改成 agent
# 侧全局 FIFO + 单 worker 串行，同一时刻 ComfyUI 里恒为 1 张（见 _worker），
# 所以这个数**不影响**显卡上的并发，只决定「一个会话能占多少排队名额」。
# 模块开头那条「N 个会话 × 2 张灌进 ComfyUI」讲的是**改造之前**的旧架构，
# 别照它反推显存余量。
#
# 2 → 5：连点 5 张不再从第 3 张起被**静默吞掉**（拒收理由要求模型「别跟对方
# 提这张图」，所以对方看不到任何解释，只觉得「只能画 2 张」）。
MAX_INFLIGHT = 5

# 全局队列上限（含正在跑的那张）。十几个群同时刷图时，不能让队列无限长——
# 排到一小时之后的图，对方早就不看了。超了就让模型回一句「排队的人太多」。
#
# 2026-10-03：10 → 20，与 MAX_INFLIGHT 2 → 5 **配套**（一个人仍最多占 1/4
# 队列）。不配套的话，两个活跃群各点 5 张就能把队列占满，第三个群一张都排不
# 进来——那正是「机器人不回消息」的观感。普通档动漫图约 20~30 秒一张，
# 20 张 ≈ 8~10 分钟，还在「对方愿意等」的范围里。
MAX_QUEUE = 20

# 重渠道（权重 > 1）在队里最多允许多少张（含正在跑的那张）。qwen 一张约
# 100 秒，3 张就把「重活独占显卡」的时间撑到 5 分钟——再多不如让模型说画不了。
# 权重本身说明不了队有多长，所以这条单独数。
#
# ⚠️ 2026-10-03 把 MAX_INFLIGHT 提到 5 时**这条故意没动**：于是对 qwen 而言
# 真正卡住的是 3，不是 5（入队时先被这条拒）。只有 qwen_image_v1 是重渠道
# （见 skills._SKILL_PRIORITY），动漫档 anima_* / hd_* 不受它影响。
MAX_HEAVY_IN_QUEUE = 3

# ── NAI 云端通道的名额（2026-10-03 拆通道时定）─────────────────
#
# 跟本地那一组**同值但独立定义**：NAI 只对开了白名单的群开放，成本是群主的
# token（按量付），将来想单独收紧改这三个数就行，不用碰本地的 20/5。
#
# 并发给 2 而不是 1：NAI 单张 5~15 秒，一个群连点两张、或者两个群同时点，
# 1 个 worker 就又排起来了——而这条通道的意义正是「云端不排本地那条队」。
# 也不能太大：同一进程里每张图都要落盘 + 审核 + 发消息，三个云端请求同时
# 回来时这些本地动作会挤在一起。
NAI_CONCURRENCY = 2
NAI_MAX_QUEUE = 20
NAI_MAX_INFLIGHT = 5

# 单张图从「真正开跑」到出图的时限（秒）。到点还没出图就中断它、让下一个上。
TASK_TIMEOUT = IMAGE_GEN_TIMEOUT

# 按渠道分级的超时（2026-10-04 加）。**一个全局数必然在两头出错**：用户看到
# 「我任务都在 80 秒以下」就想定 80，但实测（`logs/qq_bot.log` 里「渠道 X，
# seed Y，耗时 Z 秒」统计）：
#
#   anima_clear      n=294  中位 16.6  最慢 152.8
#   image_gen_v1     n=13   中位 26.3  最慢  41.6
#   hd_2_clear       n=37   中位 35.1  最慢  82.5
#   qwen_image_v1    n=58   中位 50.3  最慢 119.6
#   nffa             n=19   中位 53.7  最慢  93.4
#   hd_3_clear       n=13   中位 60.6  最慢  83.9
#   hd_3_curvy       n=3    中位 68.3  最慢  87.4
#
# 定 80 的话：anima 那 152.8s 那次会被砍（它是内存吃紧换页拖慢的，不是画不动），
# hd_3_curvy 中位就 68.3s —— 只剩 12 秒余量，**排队等待一叠加就误杀**；qwen 也有
# 约 10~15% 在 80s 以上。反过来留 180 全局，anima_clear（中位 16.6s）真卡死时用户
# 要白等 3 分钟。所以**按渠道查表**：表里没有的用 TASK_TIMEOUT（保持原行为，
# 新渠道不会因为漏配就变慢）。
#
# ⚠️ 上面那几个 anima_* / hd_2_* 的数是**下架前**的实测值，留作参照；
# 那些渠道 2026-10-07 已下架（`app/skills.ARCHIVED_SKILLS`），表里的键也删了。
#
# 键是 `job.skill`，不是渠道文件名——Job.skill 存的是渠道名。
#
# 2026-10-04 二次上调：llama.cpp 的 MiMo 常驻占显存后，ComfyUI 每张图整体慢
# 约 40%（用户实测），80/180 两档都会开始误杀——轻量最慢 152.8s 的那次
# ×1.4 ≈ 214s，重渠道 119.6s 也会顶到 180。两档同比例放大到 120/300，
# 保持「2~4 倍中位数」的原设计余量；300 正好是 .env 兜底 IMAGE_GEN_TIMEOUT
# 的新值（5 分钟），三层闸门重新对齐。
SKILL_TIMEOUTS = {
    # 轻量 SD 系：原中位 16~35s，慢四成后 22~49s，120s 仍是 2~5 倍余量
    "image_gen_v1": 120,
    # 重的一档：慢四成后中位 70~98s，80s 顶不住
    "qwen_image_v1": 300, "nffa": 300,
    # 2026-10-07：动漫族只剩三档，它比常规档重得多（画布 1024×1536 +
    # 1.5× latent 放大 + 2x 像素放大 → 3072×4608），四个画风都给 300。
    # 旧档位（anima_* / hd_fast_* / hd_2_*）已随「只剩三档」下架，
    # 不可能再被排进队列，所以那些键删掉了——留着就是读不到的死配置。
    "hd_3_clear": 300, "hd_3_curvy": 300, "hd_3_gloss": 300, "hd_3_soft": 300,
    "krea2": 300,
    # cunny（2026-10-05 加）：二段精修 2048×3072，单张估 2~4 分钟，
    # 用户拍板配 300（.env 兜底 180 对它必误杀）
    "cunny": 300,
    # miao（2026-10-05 加）：SDXL 单段 45 步 1024×1536 + 2x 放大到 2048×3072，
    # 估 90~150s，兜底 180 偏紧 —— 照 cunny 先配 300，实测出真值再收。
    "miao": 300,
}


def task_timeout(job_or_skill):
    """这个渠道单张图的出图时限（秒）。

    接 Job 或渠道名都收——`process()` 拿得到 job，判定日志与文案在异常路径上
    有时只手里一个渠道名。原 TASK_TIMEOUT 保留为**兜底默认值**，没配的渠道
    行为不变。
    """
    skill = getattr(job_or_skill, "skill", job_or_skill)
    try:
        return SKILL_TIMEOUTS.get(skill) or TASK_TIMEOUT
    except TypeError:                       # 传了个怪东西，别在热路径上抛
        return TASK_TIMEOUT

# 轮询间隔（秒）
POLL_INTERVAL = 2

# 超时中断后，等 ComfyUI 真正退场的上限（秒）。等不到就是「活着但卡住」——
# 卡在一次不返回的调用里，`/interrupt` 设的协作标志它永远读不到，于是
# `queue_running` 永远不清空。这种状态不会自己好，必须重启（见 process）。
COMFY_IDLE_WAIT = 90

# 冷却闸只认这一个渠道（见模块开头「重渠道优先度」）。写成常量而不是判断
# 「权重 > 1」，是因为冷却的必要性来自 qwen 那套权重的具体尺寸，别的重渠道
# 不一定共用同一个死因。
QWEN_SKILL = "qwen_image_v1"

# 开跑前必须先要一个「干净 ComfyUI」的渠道：值 = 至少要有的空闲显存（GB）。
#
# ⚠️ 2026-09-30 起**清空**（原为 `{"anima_2": 6.0}`）。原因是那个 6.0 门槛站不住脚：
#
#   ① 它唯一的对象 anima_2 已删掉——那套两段采样现在是**动漫渠道共用**的骨架
#      （2026-10-07 起对外只剩三档 `hd_3_<画风>`，旧档位见
#      `app/skills.ARCHIVED_SKILLS`；默认生图渠道 2026-10-06 已改成 silver，
#      见 `generate_image.T2I_DEFAULT_SKILL`），天天跑、不能每张都重启一次 ComfyUI。
#      （2026-10-01 另加了高清档，2026-10-07 收缩成只剩三档——画布 1024×1536、
#      1.5× latent 放大 + 2x 像素放大 → 3072×4608，比常规档重得多——
#      但**仍然不设门槛**，理由同 ②。）
#
#   ② 更要紧的是：**实测证明那个门槛本身就是过保守的**。翻 09-30 的
#      comfyui_8188.log，新的两段（10+5）工作流在**同一个脏进程**上连跑 8 次
#      ——02:00、02:02、02:20、02:24、02:27:44、02:27:56、02:28:07——**一次都没
#      重启、一次都没崩**，最密的两张只隔 1 秒（02:27:55.617 完 → 02:27:56.030
#      下一个 got prompt）。每次都是正常的两段 staged（280 patches → 0 patches）
#      + `Prompt executed in 10.7~11.7 秒`。也就是说「脏状态 5.5GB 装不下 5.4GB
#      峰值」这个担心是**假的**——ComfyUI 自己的 DynamicVRAM 会在两段之间把
#      不需要的权重换出去，峰值并没有真的叠加到 5.4GB。
#
#   ③ 真正崩过的那次（01:57:30 `CUDA error: unspecified launch failure`）跑的是
#      **旧的单段 15 步**工作流，跟两段采样没有因果关系。旧结论把两件不相关的事
#      串成了一条因果链。
#
# 所以现在没有任何渠道需要「预备重启」。将来若真要加回来，**必须先看日志里的
# staged 峰值和真实崩溃点**，别按权重表算 GB 往上堆。
CLEAN_START_SKILLS = {}

# 按渠道的**事前内存**水位（GB）：开跑前系统可用内存低于它就重启 ComfyUI。
# 与 CLEAN_START_SKILLS（显存）分表，因为两者是独立的两条资源，量级也不同。
#
# ⚠️ **2026-10-04 实测后保持为空 —— 这条路被数据否决了，别按直觉填回去。**
# 原本想给 qwen 挂 8.0GB（它的权重 10.5GB，而 03:40 那次提交时内存余 4.0GB）。
# 拿日志里 275 次「开跑前水位」与后续成败配对统计，判别力接近零：
#
#   提交时内存 <4GB： 75 张，失败 1 张（1%）
#   提交时内存 ≥4GB：199 张，失败 8 张（4%）   ← 反而更高
#
# 而且 275 次采样里**最高只有 7.2GB**，<8GB 占 100% —— 定 8.0 等于每张 qwen 都
# 重启，那只是把「重渠道」换了个名字，用户 10-04 刚把它拍板关掉。
#
# 更要命的是失败本身跟提交时水位无关：9 次失败里 **8 次发生在内存还够的时候**
# （提交时 4.0 / 4.3 / 4.8 / 5.1 / 6.0GB）。真正的死因是**跑起来之后** qwen 一口
# 吃掉 10GB+ 把内存压到 0.1~0.5GB，ComfyUI 被系统杀 —— 那一刻已经提交了，水位
# 读数是事后诸葛亮。
#
# 结论：事前水位拦不住，能拦住的只有**降低峰值**（ComfyUI 启动加 `--fast-disk`，
# 权重走 NVMe 而不是全压内存）或**换更大的物理内存**。真要加回来，必须先拿出
# 「某阈值下失败率显著更高」的证据。
CLEAN_START_RAM = {}

# NAI 的两个渠道名（2026-10-03 加 `nai_wide` 横版）。所有「是不是 NAI」的判断
# 都走这个元组——`job.skill` 存的是**真实渠道名**，云端分支靠它区分横竖。
NAI_SKILLS = ("nai", "nai_wide")

_lock = threading.Lock()


class _Channel:
    """一条独立队列 + 自己的 worker 线程。

    2026-10-03 之前这里只有一条队列，NAI 也得跟着本地图排——可它走的是云端，
    本机一帧都不渲染，排在十几张本地图后面纯属白等。现在按「占不占本机显卡」
    分成两条：

    - **本地通道**（ComfyUI，`local=True`）：权重排序、冷却窗、换渠道 /free、
      内存水位重启**全归它**——那些机制的存在理由都是显卡（见模块开头）。
    - **NAI 通道**（云端，`local=False`）：`workers` 个 worker 并行跑，不排序、
      不冷却。本机只负责落盘、审核、发消息。

    两条通道共用同一把 `_lock`：临界区都只有几行、没有磁盘 IO，为并行再拆一套
    锁得不偿失。跨通道的查询（查重、回执、每会话在途）自己遍历两条。
    """

    def __init__(self, name, workers=1, local=True, max_queue=MAX_QUEUE,
                 max_inflight=MAX_INFLIGHT, max_heavy=MAX_HEAVY_IN_QUEUE):
        self.name = name
        self.local = local
        self.workers = workers
        self.max_queue = max_queue
        self.max_inflight = max_inflight
        self.max_heavy = max_heavy
        self.queue = collections.deque()   # 待跑的任务（不含正在跑的）
        self.running = []                  # 正在跑的（本地恒 0/1 个）
        self.per_session = {}              # (target, target_id) -> 在途张数（含排队）
        self.seq = 0                       # 入队序号，同权重的按它先进先出
        self.wake = threading.Event()      # 有新任务入队时戳一下 worker
        self.worker_started = False
        # 下面三个只有本地通道用（全是为显卡发明的，见「重渠道优先度」）：
        self.heavy_done_at = 0.0           # 上一张重渠道**跑完**的时刻
        self.last_skill = None             # 上次提交给 ComfyUI 的渠道（判要不要 /free）
        self.same_run = 0                  # 同渠道已连续提交的张数（连画 N 张后 /free）
        self.last_restart_try = 0.0        # 上次**尝试**重启 ComfyUI 的时刻（防抖）

    def depth(self):
        """这条通道还有几个在等 / 在跑。"""
        return len(self.queue) + len(self.running)


_COMFY = _Channel("comfy", workers=1, local=True)
_NAI = _Channel("nai", workers=NAI_CONCURRENCY, local=False,
                max_queue=NAI_MAX_QUEUE, max_inflight=NAI_MAX_INFLIGHT)

# 最近跑完（含失败）的几张图，按会话可查——给模型回答「刚才那张画好没有」用。
# 有界、进程内、重启即空：它只是一条**回执**，不是账本，不需要落盘。
# 两条通道共用（模型关心的是「这个会话的图出没出」，跟哪条通道无关）。
_RECENT_MAX = 30
_recent = collections.deque(maxlen=_RECENT_MAX)


def _channel_of(skill):
    """这个 skill 该进哪条通道。NAI 的两个渠道名走云端，其余全归本地。"""
    return _NAI if skill in NAI_SKILLS else _COMFY


def _channel_of_job(job):
    """任务所属通道。

    `job.chan` 是入队时定的；测试里手搓的 Job 没带，就按 skill 反推——这样
    `_finish` 对两条通道是同一份代码，不用调用方再传一遍。
    """
    return getattr(job, "chan", None) or _channel_of(getattr(job, "skill", None))


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


def _heavy_ahead(chan=None):
    """这条通道里已经在等或正在跑的重渠道有几张（含正在跑的）。"""
    chan = chan or _COMFY
    return sum(1 for j in chan.queue if j.weight > 1) \
        + sum(1 for j in chan.running if j.weight > 1)


class Job:
    """一次生图任务。

    QQ 侧提交完即返回（图由 worker 直接发回原会话）；网页侧用 wait() 阻塞
    等结果——注意等的是「排队 + 出图」全程，排队时间不由我们控制。
    """

    def __init__(self, target, target_id, workflow, skill=None, weight=1, seq=0,
                 nai_i2i=None, tag=None, prompt=None, intent=None, seed=None,
                 chan=None, landscape=False):
        self.target = target
        self.target_id = target_id
        self.workflow = workflow
        # 生图渠道：决定进哪条通道（见 _channel_of）、要不要先 /free、权重多少
        self.skill = skill
        # 所属通道（enqueue 时定）。手工构造的 Job 这里是 None，_channel_of_job
        # 会按 skill 反推——测试里那一堆 Job(...) 不用改。
        self.chan = chan
        # 这张图的种子（提交那一刻就定死，见 generate_image._resolve_seed）。
        # **必须在入队时快照**：worker 线程拿不到工作流里那个数（seed 已经混在
        # 几十个节点里），而 caption 要贴它、账本要存它，两处都得用同一个值。
        # None = 这次没带种子（老调用方 / NAI 那种云端图），caption 上就不写。
        self.seed = seed
        # 这次提交的「意图指纹」——模型给的原始 prompt + skill + lora（见
        # generate_image._intent_key）。用来识别「同一件事被提交了两遍」。
        # 不能拿 workflow 当指纹：那里面填进了随机 seed，两次一模一样的
        # 请求也会算出两个不同的值。
        self.intent = intent or ""
        # 模型写的那段原始提示词（**替换进工作流之前的**）。图发出去之后要连
        # 编号一起记进账本，否则以后查编号只能查到空——工作流里那份是给
        # ComfyUI 的，含固定前缀和一堆节点，不适合当「这是什么图」的答案。
        self.prompt = prompt or ""
        self.weight = weight        # 队列权重（默认 1 = 普通；qwen 是 5）
        self.seq = seq              # 入队序号，同权重的按它先进先出
        self.created = time.time()  # 入队时刻：状态后台用它算「已等 N 秒」
        # 真正开跑的时刻（process 一进来就打）。只用来算「这张画了多久」——
        # 从 created 算会把排队时间也算进去，排在第 5 位的那张看着像画了十分钟。
        self.started = None
        # 这张图的编号（形如 HT-20261001-074112-384）。发图时当 caption 贴在
        # 图片上，群友引用那条消息时编号会跟着引用回到模型眼前
        # （见 app/image_log.py）。
        # **在入队这一刻定下来**：worker 发图要用它，之后写账本也要用它，两处
        # 必须是同一个值。传 tag 只给测试固定编号用。
        self.tag = tag or image_log.new_tag()
        # NAI 图生图的入队时快照：{"image": 纯base64, "strength": 重绘强度}。
        # 必须快照——worker 线程读不到 qq_api 线程本地的「本轮引用图」。
        self.nai_i2i = nai_i2i
        # 横屏（2026-10-07）：入队时快照的「本轮原话说了横屏/横版/横图」。
        # 跟上面 nai_i2i 是**同一个理由**：`process()` 跑在常驻的 image-worker
        # 线程里，而 qq_api 的上下文是 threading.local()、只在 qq_bot 的会话
        # 线程绑过 → worker 里读 `current_turn_text()` 恒为 None。
        # （2026-10-07 实测：主线程绑 'silver 横屏 一个女孩'，子线程读出 None。
        #   教训：判据一律在入队线程算，别在 worker 里现读。）
        self.landscape = bool(landscape)
        self.waits = 0              # 被冷却 / 被插队推回过几次（只为日志）
        self.skill_done = False     # 已经成功跑完一张？决定跑完要不要开冷却
        self.prompt_id = None
        self.entry = None           # 出图后的 history entry
        self.error = None           # 失败原因（网页侧 wait 时抛出来）
        # 这张有没有扣过私聊每日额度（generate_image._charge_quota 打的标记）。
        # 只有它为真、且最后**没出图**，_finish 才退还一个名额。默认 False：
        # 网页端和群聊都不扣，测试里手搓的 Job 也不扣。
        self.quota_charged = False
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
    """这个会话还有几张在途（含排队中）——**两条通道一起算**。

    模型看到的是「这个人还有几张图在路上」，跟他点的是本地还是云端无关：
    上面那句「对方说没收到图时先想这几张」（见 agents.private_quota_line）
    数漏一张就会让它把「在跑的 NAI 图」当成没画过，再提交一遍。
    """
    key = _key(target, target_id)
    with _lock:
        return sum(c.per_session.get(key, 0) for c in (_COMFY, _NAI))


def queue_depth():
    """两条通道加起来还有几个在等 / 在跑（总量，供测试与状态页用）。"""
    with _lock:
        return _COMFY.depth() + _NAI.depth()


def nai_depth():
    """NAI 通道现在有几张在跑 / 在排（给状态栏用的本地事实）。

    ComfyUI 的排队数靠 /queue 探测（comfy_status），NAI 是云端请求，
    ComfyUI 那边根本看不见——机器人想知道「NAI 画完没有」只能看这里。
    并发是 `NAI_CONCURRENCY`（默认 2），所以 running 可能是 0/1/2。
    横竖两个渠道一起算：问的是「NAI 忙不忙」，跟出的是横是竖无关。
    """
    with _lock:
        return len(_NAI.running), len(_NAI.queue)


def recent_outcomes(target, target_id, limit=3):
    """本会话最近 limit 张图的结局（旧的在前，最新的在后）。

    只认「本会话」的：别的群/私聊画了什么跟当前对话无关，混进来只会让模型
    串台。target 为 None（网页侧）时没有会话概念，返回空。
    """
    if target is None:
        return []
    key = _key(target, target_id)
    with _lock:
        picked = [r for r in _recent
                  if _key(r["target"], r["target_id"]) == key]
    if limit and limit > 0:
        picked = picked[-limit:]
    return picked


def _inflight_of(target, target_id, limit=3):
    """本会话**还没出图**的任务（正在跑 + 排队中），按入队顺序，旧的在前。

    回执原先只报「已经跑完的」，模型于是分不清「刚提交、还在跑」和「早就
    跑完」——它会把上一条已完成当成对方刚发的那张。把在途任务也报出来，
    「这张到底提交过没有」就从推理题变成看得见的事实。
    """
    key = _key(target, target_id)
    with _lock:
        items = [(j, "排队中") for c in (_COMFY, _NAI) for j in c.queue
                 if _key(j.target, j.target_id) == key]
        for c in (_COMFY, _NAI):
            items += [(j, "正在跑") for j in c.running
                      if _key(j.target, j.target_id) == key]
    # 按**入队时刻**排，不按 seq：seq 是每条通道各自发号的，跨通道没有可比性。
    items.sort(key=lambda pair: pair[0].created)
    if limit and limit > 0:
        items = items[-limit:]
    return items


def recent_line(target, target_id, limit=3):
    """把「已完成」和「还没出图」渲染成一段给模型看；两者都没有就返回空串。

    为什么需要它（2026-09-29 用户提）：图由 worker 直接发回会话，模型在
    enqueue 拿到「已经排上队了」之后就**再也收不到任何回执**——它不知道图
    出没出，于是老说「我再帮你跑一张」。这段就是那条回执，跟着 extra_context
    每轮现取现用（出流即弃，不写回 history）。

    ⚠️ 2026-09-29 二次修（用户报「AI 会撒谎，不知道前面的生图需求完成没有」）。
    原版只说「最近 3 条已出图」，**不带时间、不带在途任务**：模型会把上一条的
    成功读成「对方刚发的那张也成了」；结尾那句「别再问『要不要重画』」更糟——
    对方说「我没看到」时它反而不敢重画，只会让人「往上翻」。实测当天 130 条
    声称出图的回复里 **32 条整轮没调过 generate_image**。现在三处改动：
    ① 每条带完成时刻；② 在途任务单列一段；③ 明说「没列出来的 = 还没提交」，
    并把「对方说没看到」的正确动作写清楚。
    """
    if target is None:
        return ""
    done = recent_outcomes(target, target_id, limit)
    doing = _inflight_of(target, target_id, limit)
    if not done and not doing:
        return ""
    lines = ["[最近生图]（只列你在这个会话提交过的，按提交先后，最新在后）"]
    if done:
        bits = []
        for i, r in enumerate(done, 1):
            when = time.strftime("%H:%M", time.localtime(r.get("ts") or 0))
            skill = r["skill"] or "默认"
            if r["ok"]:
                bits.append("%d) %s 已出图（%s）" % (i, when, skill))
            else:
                why = r["err"]
                bits.append("%d) %s 失败（%s%s）"
                            % (i, when, skill, ("：" + why) if why else ""))
        lines.append("已完成：" + "；".join(bits))
    if doing:
        bits = []
        for i, (j, state) in enumerate(doing, len(done) + 1):
            when = time.strftime("%H:%M", time.localtime(j.created))
            bits.append("%d) %s 提交（%s），%s"
                        % (i, when, j.skill or "默认", state))
        lines.append("**还没出图**：" + "；".join(bits))
    lines.append(
        "以上是你真的提交过的。对方贴了新 prompt 但这上面没多出新条目 = 这张"
        "还没提交，别说「出了/发了」；对方说没看到就是真没出图，直接重跑一张，"
        "别让他自己往上翻。")
    return "\n".join(lines)


# 守卫判「模型是不是在空口承诺」时，多久以内出过图还算「它没瞎说」。
RECENT_DONE_WINDOW = 900.0


def recent_activity(target, target_id, within=RECENT_DONE_WINDOW):
    """本会话「还在跑 + 刚出图」的条数。给 agent 的生图空头承诺守卫用。

    那道守卫的判据是「**这一轮**没调 generate_image 却声称在画图」，而
    「这一轮」是 run 级的（见 agent._IMAGE_CLAIM_NUDGE 上方那段注释）。QQ
    路径下提交完立刻返回（generate_image._generate_image），所以「上一轮提交、
    这一轮汇报进度」本来就是常态——不查队列的话，如实汇报会被判成空头承诺
    退回重来，而 nudge 里那句「那张图根本不存在」会让模型真的再提交一遍。

    实测（2026-10-01 08:13，logs/qq_bot.log）：模型说「上一张三档（hd_3）还在
    跑 出图时间本来就比二档长不少」，被判成空头承诺；下一轮它真的又调了一次
    generate_image，队列里多出一张 hd_3。
    """
    if target is None:
        return 0
    key = _key(target, target_id)
    now = time.time()
    with _lock:
        n = sum(1 for c in (_COMFY, _NAI)
                for j in list(c.queue) + c.running
                if _key(j.target, j.target_id) == key)
        n += sum(1 for r in _recent
                 if _key(r["target"], r["target_id"]) == key
                 and now - (r.get("ts") or 0.0) <= within)
    return n


def find_pending_duplicate(target, target_id, intent):
    """本会话里「还没出图、意图又完全一样」的任务；没有就返回 None。

    只认**在途**的（排队中 / 正在跑）：已经出过图的那张，对方看过之后再点一次
    同样的是有意为之——实测 2026-10-01 08:18 有人明确说「三档同一份词条原样
    重跑一张 你看看这次细节对不对」，把这种情况当重复拦下等于把正常需求堵死。
    在途的才算重复：对方什么都还没看到。
    """
    if target is None or not intent:
        return None
    key = _key(target, target_id)
    with _lock:
        for c in (_COMFY, _NAI):
            for j in list(c.queue) + c.running:
                if _key(j.target, j.target_id) == key and j.intent == intent:
                    return j
    return None


def ahead_of(job):
    """这个任务前面还有几张（含正在跑的那张）。已经开跑就返回 0。

    按**出队顺序**数，不是按入队顺序：后入队的普通任务会插到先入队的 qwen
    前面，模型报给对方的「前面还有 N 张」得跟它实际要等的一致。

    只数**自己那条通道**的（2026-10-03 拆通道后）：NAI 不会再排在本地图后面，
    这里要是把两条加在一起，模型就会对一张其实立刻开画的 NAI 图说「前面还有
    十几张」——比不报还糟。
    """
    chan = _channel_of_job(job)
    with _lock:
        if job not in chan.queue:
            return 0
        order = sorted(chan.queue, key=_order)
        return order.index(job) + len(chan.running)


def snapshot():
    """给状态后台用的队列快照（只读，不改任何状态）。

    返回 {"running": {...}|None, "queued": [ {...} ], "depth": N, "nai": {...}}。
    前三个键还是**本地通道**的老形状（前端 web/status.html 直接读它们，别改），
    "nai" 是云端通道的同构视图（它的 running 是列表——并发 NAI_CONCURRENCY），
    "depth" 是两条通道加起来的。
    每张暴露 target/target_id/skill/weight/已等秒数/前面还有几张/提示词预览
    ——正是「谁在排队、排了多久」这个问题需要的全部字段。

    ⚠️ 不能在持锁时调 ahead_of()——_lock 是普通 Lock（非可重入），
    嵌套获取会直接死锁。位次在这里按出队顺序就地算。
    """
    now = time.time()
    with _lock:
        def _j(job, ahead):
            wf = job.workflow
            return {
                "target": job.target,
                "target_id": job.target_id,
                "skill": job.skill or "",
                "weight": job.weight,
                "age": round(now - job.created, 1),
                "ahead": ahead,
                "tag": job.tag,
                "prompt": (wf[:60] if isinstance(wf, str) else ""),
            }

        def _view(chan, single):
            order = sorted(chan.queue, key=_order)
            running = [_j(j, 0) for j in chan.running]
            if single:                      # 本地通道：老前端要的是对象或 None
                head = running[0] if running else None
            else:
                head = running
            return {
                "running": head,
                "queued": [_j(j, len(running) + i)
                           for i, j in enumerate(order)],
                "depth": chan.depth(),
            }

        comfy = _view(_COMFY, single=True)
        return {
            "running": comfy["running"],
            "queued": comfy["queued"],
            "depth": _COMFY.depth() + _NAI.depth(),
            "nai": _view(_NAI, single=False),
        }


def enqueue(target, target_id, workflow, skill=None, nai_i2i=None, prompt=None,
            intent=None, seed=None, resample_fn=None, landscape=None):
    """把一张图排进**它该去的那条通道**的队列，返回 (job, reason)。

    prompt 是模型写的那段原始提示词，只用来**出图后记进账本**（编号 → 提示词，
    见 app/image_log.py）；生图本身用的是 workflow，这里传不传都不影响出图。

    reason 非 None 表示没接（此时 job 为 None），它是一句可以直接转述给对方
    的话。三种拒收：这条队排太长、这个会话自己排太多、重渠道已经排了太多。

    skill 用来定三件事：进哪条通道（NAI 走云端那条，见 _channel_of）、队列
    权重（`skill_priority`，qwen 排最后）和「换渠道先 /free」的判断
    （见 _maybe_release_for_switch）。
    不传 skill 时的行为与从前完全一致（权重 1、从不主动释放）——老调用方
    不受影响。nai_i2i 只在 NAI 图生图时传（入队时快照的源图 base64 + 强度，
    见 Job.nai_i2i）。intent 是这次请求的意图指纹，只用于查重（见
    find_pending_duplicate），不传就是「不参与查重」。seed 是这张的种子，
    只在**发图那行 caption 和出图后的账本**里用（生图本身用的是 workflow 里
    那个已经填好的数），不传就是没种子、caption 上不多那段。
    """
    key = _key(target, target_id)
    chan = _channel_of(skill)
    weight = skill_priority(skill)
    # 横屏（2026-10-07）：判据**只能在这里**算——入队线程就是 qq_bot 的会话
    # 线程，qq_api 的上下文还绑着；等 worker 拿到这个 job 时线程本地变量早就
    # 不是本轮了（见 Job.landscape 那段）。所以先拍板、存进 job。
    # landscape=None = 按本轮原话自动判（QQ 侧那两个调用方走这条）；显式
    # True/False 留给确认闸——它是在**下一条消息**（「好」）里重入队的，
    # 那时候的原话里根本没有「横屏」这个词，只能靠当初快照的值。
    if landscape is None:
        landscape = turn_is_landscape()
    with _lock:
        depth = chan.depth()
        if depth >= chan.max_queue:
            return None, ("现在排队的人太多了（前面还有 %d 张），这一张先不画。"
                          "别跟对方提这张图，当没画过，接着把话说完。" % depth)
        cur = chan.per_session.get(key, 0)
        if cur >= chan.max_inflight:
            return None, ("这个会话已经排着 %d 张了，画完这些再说。"
                          "不要跟对方提这张图，当没画过，接着把话说完。" % cur)
        if weight > 1 and _heavy_ahead(chan) >= chan.max_heavy:
            # 重渠道一张就把显卡占满好几分钟。排队太长不如让它说一句
            # 「我现在画不了」——总好过半小时后发一张对方早忘了的图。
            return None, ("画图那个通道正忙着（已经在排 %d 张重的），这一张先不画。"
                          "别跟对方提这张图，当没画过，接着把话说完。"
                          % chan.max_heavy)
        chan.seq += 1
        job = Job(target, target_id, workflow, skill, weight, chan.seq,
                  nai_i2i=nai_i2i, prompt=prompt, intent=intent, seed=seed,
                  chan=chan, landscape=landscape)
        # 随机口令的「被拦静默重抽」钩子（2026-10-05）：resample_fn 是无参
        # 可调用体，被审核拦下时由 worker 调它换一条新提示词，同 job 重跑。
        # 最多重抽 2 次（3 尝试）；普通生图不传，拦截行为与从前完全一致。
        job.resample_fn = resample_fn
        job.retry_left = 2 if resample_fn else 0
        job.audit_silent = bool(resample_fn)
        chan.queue.append(job)
        chan.per_session[key] = cur + 1
    if weight > 1:
        # 入队就记一行：模型选渠道的决策只有在这里才看得见，出问题时先查这行。
        log.info("重渠道 %s 入队（%s %s，权重 %d，前面 %d 张）",
                 skill, target, target_id, weight, ahead_of(job))
    chan.wake.set()
    _ensure_worker()
    return job, None


def _ensure_worker():
    """确保**每条通道**的 worker 线程都在跑，缺哪条补哪条。

    保持零参数是刻意的：测试里到处把它替成 `lambda: None`（见
    tests/test_image_jobs.py 的 _COMFY_IO_BLOCKERS），改签名会一次性弄红
    七八个用例；而「把所有通道的 worker 都拉起来」本来就是要的行为——闲着的
    线程只是每秒醒一次看一眼自己的队列。
    """
    for chan in (_COMFY, _NAI):
        with _lock:
            if chan.worker_started:
                continue
            chan.worker_started = True
        try:
            for i in range(chan.workers):
                threading.Thread(
                    target=_worker, args=(chan,), daemon=True,
                    name="image-worker-%s-%d" % (chan.name, i)).start()
        except Exception:
            with _lock:             # 线程没起来就别占着「已启动」的位
                chan.worker_started = False
            raise


def _next_ready(chan=None):
    """挑这条通道下一个该跑的任务（**调用方必须已持 _lock**）；没有返回 None。

    「重渠道不许连跑」只对**本地通道**有意义（`chan.local`）：那条规则要防的是
    显存残留撞上下一张的模型装载，NAI 走云端、本机没有显存可等，冷却窗对它
    纯粹是白等，所以云端通道不参与。

    本地那段逻辑：只要**上一张成功的是重渠道**且还没过冷却窗，
    队里的重渠道就一张都不许开跑——普通渠道照常放行（这正是冷却窗的意义：
    把间隙让给别人）。全是重渠道、又都在冷却里时返回 None，worker 回去等。

    两个容易踩的点：

    - 判据读的是 `chan.heavy_done_at`（跑**完**的时刻）而不是提交时刻：要防的是
      「前一张卸载下来的那几秒正好撞上后一张的模型装载」，从提交起算会把窗口
      整个错开。
    - 冷却窗一过就**立刻**把队里的重渠道重新排好（按 _order 取最小），所以
      冷却结束不需要任何额外的唤醒信号——worker 每秒醒一次，下一轮就看见了。
      这也意味着「冷却中的重渠道不占用 ahead_of 的名额」是自动成立的。
    """
    chan = chan or _COMFY
    if not chan.queue:
        return None
    if chan.local and _cooling(chan):
        ready = [j for j in chan.queue if j.weight <= 1]
        if not ready:
            return None
        return min(ready, key=_order)
    return min(chan.queue, key=_order)


def _cooling(chan=None):
    """这条通道的重渠道现在在冷却里吗（排队阶段的闸）；调用方已持 _lock。"""
    chan = chan or _COMFY
    if QWEN_COOLDOWN <= 0 or chan.heavy_done_at <= 0:
        return False
    return (time.time() - chan.heavy_done_at) < QWEN_COOLDOWN


def _move_back(chan, job):
    """把一个任务挪到队尾（冷却没到点的重渠道用）；调用方已持 _lock。

    不是 `job.seq = _seq+1` 了事——那样所有冷却里的重渠道会**共享同一个序号**，
    重排时的先后就变成集合顺序（不确定）。真挪到队尾：重新取号并移到 deque
    尾部，让「谁先被推回去谁先出来」稳定下来。
    """
    try:
        chan.queue.remove(job)
    except ValueError:
        return                          # 已经被别的路径取走了
    chan.seq += 1
    job.seq = chan.seq
    job.waits += 1
    chan.queue.append(job)
    log.info("重渠道 %s 让行（第 %d 次）：上一张 %s 刚跑完不到 %.0f 秒，"
             "先让普通渠道上", job.skill, job.waits, QWEN_SKILL, QWEN_COOLDOWN)


def _take_nowait(chan=None):
    """立刻取一个任务；没有 / 都还在冷却里就返回 None。

    不传 chan 时按本地通道取——老调用方（和测试里的 `_take_nowait()` 当
    worker 用）行为不变。
    """
    chan = chan or _COMFY
    with _lock:
        if not chan.queue:
            return None
        # 上一张要是重渠道，现在又还在冷却里，先把它挪到队尾——
        # 否则它会是 _order 的最小项，直接被取走，让行规则形同虚设。
        if chan.local and _cooling(chan):
            for job in [j for j in chan.queue if j.weight > 1]:
                _move_back(chan, job)
        job = _next_ready(chan)
        if job is None:
            return None
        try:
            chan.queue.remove(job)
        except ValueError:
            return None
        chan.running.append(job)
        return job


def _take(chan):
    """取这条通道的下一个任务；没有就阻塞等（每秒醒一次，保证收得到入队信号）。

    同一通道有多个 worker（NAI 是 2 个）时，它们抢同一个 `chan.wake`：Event
    被其中一个 clear 掉之后，其余的也会在 1 秒内自然醒来重新看一眼队列——
    延迟上限就是这一秒，不值得为它换 Condition。
    """
    while True:
        job = _take_nowait(chan)
        if job is not None:
            return job
        chan.wake.wait(1)
        chan.wake.clear()


def _worker(chan):
    """这条通道的工作线程：本地一张接一张串行；云端 N 个线程并行。"""
    while True:
        job = _take(chan)
        try:
            process(job)
        except Exception:
            log.exception("生图任务处理时抛异常 %s %s", job.target, job.target_id)
        finally:
            _finish(job)
        if not chan.local:
            continue
        # 每跑完一张看一眼内存——ComfyUI 的常驻内存是按张涨的（见
        # _maybe_restart_for_ram）。放在这里而不是 _take 之前：_take 会阻塞
        # 等新任务，在那儿检查就变成每秒一次了。**只有本地通道要做**：
        # 云端那张图跟 ComfyUI 的内存一个字节的关系都没有。
        _maybe_restart_for_ram()


def _maybe_release_for_switch(job):
    """渠道变了就先让 ComfyUI 把上一个渠道的模型卸掉；同渠道连画满 N 张也放一次。

    为什么不能只靠异常路径那条规则（见模块开头「关键取舍」）：那条规则的理由
    是「正常跑完继续用同一个模型更快」——**换渠道时这个理由不成立**，旧渠道的
    模型下一张根本用不上，留着只是把显存和内存占住。2026-09-27 实测：anima
    单阶段连跑两张都正常，紧接着同一个 ComfyUI 会话里跑 qwen（文本编码器
    6GB + unet 4.5GB），采样到一半就 TDR，ComfyUI 直接变成僵尸。

    skill 为 None（老调用方没传）时整个函数是空操作，行为与从前完全一致。

    **同渠道连画释放**（2026-10-05 加，`COMFY_RELEASE_AFTER_SAME`，0=关）：
    ComfyUI 从不把上一张清干净——每张跑完 `Unloaded partially: ... remains
    loaded`，残留 1.6~2.1GB 一路叠上去。qwen 权重 10.5GB / 显存 11.94GB，
    叠两张之后 UNet 装不下、每步从内存搬 4.5GB，速度 ×8、撞超时（用户实录：
    第 3 张起超级慢→卡死）。**同一个渠道连续提交满 N 张后，下一张提交前
    直接重启 ComfyUI**（2026-10-05 用户拍板：不打 /free——残留叠进换页态
    后 /free 救不回来，重启是唯一能把显存+内存都真正还回去的手段；
    约 60~90 秒不能出图，用户知情选定）。
    """
    chan = _COMFY
    skill = job.skill
    if skill is None:
        return
    prev = chan.last_skill
    chan.last_skill = skill         # 无论打不打 /free 都要记，否则会反复触发
    if prev is None:                # 刚重启完/冷启动，这张算新一轮第 1 张
        chan.same_run = 1
        return
    if prev == skill:
        # 同渠道连画：数满 N 张就整个重启 ComfyUI（显存+内存清零）。
        if 0 < COMFY_RELEASE_AFTER_SAME <= chan.same_run:
            log.info("同渠道 %s 已连画 %d 张，下一张提交前彻底重启 ComfyUI",
                     skill, chan.same_run)
            chan.same_run = 1       # 重启后一个模型都不在，这张算新一轮第 1 张
            if not _restart_comfy():
                # 重启被拒/没回来：退回打一发 /free，至少把显存残留清了，
                # 不能让队列原地卡死。
                log.warning("ComfyUI 重启失败，退回 /free 清显存")
                _report_and_free()
        else:
            chan.same_run += 1
        return
    chan.same_run = 1               # 换渠道，重新计数
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

    它同时是「提交前水位」的**唯一记录点**（2026-10-03 加）：日志里原先所有
    内存读数都是 `_report_and_free()` 打的，而那是 **POST /free 之后**的数
    （模型已经卸了），不是这张图开跑前的数——于是 `COMFY_MIN_FREE_RAM_GB`
    该定 3.0 还是 5.0 一直没有依据。同一个 `/system_stats` 响应里就有
    `system.ram_free`，白拿，就在这里记一行：把超时/变慢的图和它一对照，
    死区就能量出来，不用继续猜。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/system_stats", timeout=10)
        resp.raise_for_status()
        stats = resp.json()
        devs = stats.get("devices") or [{}]
        vram = devs[0].get("vram_free", 0) / 2 ** 30
        ram = (stats.get("system") or {}).get("ram_free", 0) / 2 ** 30
        log.info("开跑前水位：显存余 %.1fGB，内存余 %.1fGB", vram, ram)
        return vram
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

    阈值默认 2.0GB（.env 可覆盖）：anima 跑完还剩约 4.9GB、qwen 约 3.1GB，
    都不该动它——真掉到 2GB 以下才是「残留把空间吃掉了」。正常连画不受影响。

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
    # 任何一次重启都重置防抖（2026-10-03）：超时那条路现在也会重启，不记的
    # 话 worker 紧接着的 _maybe_restart_for_ram 会再重启一次，白等一轮。
    _COMFY.last_restart_try = time.time()
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
                with _lock:
                    _COMFY.last_skill = None
                    _COMFY.same_run = 0
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
    if COMFY_MIN_FREE_RAM_GB <= 0:
        return                       # 功能关掉了
    now = time.time()
    if now - _COMFY.last_restart_try < COMFY_RESTART_MIN_GAP:
        return                       # 刚试过，别反复折腾（也防失败后每张刷日志）
    free = _free_ram_gb()
    if free is None or free >= COMFY_MIN_FREE_RAM_GB:
        return
    # 成败都记：不记的话重启被拒时会每张图重试一次，日志刷屏还白等 3 秒。
    _COMFY.last_restart_try = now
    log.warning("系统可用内存只剩 %.1fGB（低于 %.1fGB 水位），重启 ComfyUI 释放",
                free, COMFY_MIN_FREE_RAM_GB)
    _restart_comfy()


def _maybe_restart_for_clean_start(job):
    """这个渠道要「干净的 ComfyUI」才跑得动：显存/内存不够就先重启一次。

    跟 _maybe_restart_for_ram 的区别：那条是**事后**（跑完一张发现内存被啃低
    了才补），这条是**事前**（明知道这个渠道要 10.5GB，先看够不够再说）。只有
    事前才拦得住——事后重启的时候那张图已经超时失败了。

    2026-10-04：新增**内存**这一路。原来的 CLEAN_START_SKILLS 只看显存，而 qwen
    崩的是内存——10-04 03:40 那次现场：提交时内存余 4.0GB（高于全局 3.0 水位，
    放行），跑起来 qwen 一口吃掉 10GB+，03:40:55 掉到 0.5GB、ComfyUI 被系统杀，
    180s 超时失败。全局 3.0GB 那条水位是给 5.4GB 的 anima 定的，**跟 qwen 不在
    一个量级**（当天日志里「内存余 0.1/0.3/0.5GB」反复出现，全是 qwen）。

    两个「不折腾」的早退：读不到数（None）时什么都不做——那种情况下
    ComfyUI 多半已经不在了，重启请求同样发不出去，还不如照常提交，让 _notice
    去说一句「ComfyUI 没在线」，比在这里白等 180 秒诚实。

    每张图最多重启一次（只在提交前判一次），所以不会退化成重启循环——即使
    重启完还是不够，也只是这一张照常提交、照常可能超时。
    """
    skill = job.skill or ""
    need_vram = CLEAN_START_SKILLS.get(skill) or 0
    need_ram = CLEAN_START_RAM.get(skill) or 0
    if need_vram <= 0 and need_ram <= 0:
        return
    vram, ram = _free_vram_gb(), _free_ram_gb()
    # 水位取「更紧的那个先满足」：两边都要够才放行，但先报出来的是差得最远的
    # 那个，日志里一眼能看出到底是哪一边卡住了。
    lack = ""
    if need_vram > 0 and vram is not None and vram < need_vram:
        lack = "要 %.1fGB 显存，现在只剩 %.1fGB" % (need_vram, vram)
    elif need_ram > 0 and ram is not None and ram < need_ram:
        lack = "要 %.1fGB 内存，现在只剩 %.1fGB" % (need_ram, ram)
    if not lack:
        return
    log.info("%s 开跑前%s——先重启 ComfyUI 要一个干净状态再跑", skill, lack)
    _restart_comfy()


def _send_image_bytes(target, target_id, path):
    """直接发一张本地文件（NAI 这种不走 ComfyUI 的图用）。"""
    from app import qq_api
    qq_api.send_image(target, target_id, path)


def _process_nai(job):
    """NAI 云端生图分支：完全不碰 ComfyUI。

    图由 NovelAI 的服务器直接出（群主独立 token），worker 只负责把字节发回
    原会话。job.workflow 在这里其实是 prompt 字符串（enqueue 时这么塞的）。
    """
    try:
        from app import nai
        i2i = getattr(job, "nai_i2i", None)
        if i2i:
            # 图生图的尺寸**跟着源图走**（prepare_image 算好并快照进 i2i），
            # 跟渠道横竖无关；缺尺寸的老快照退回竖版兜底。
            png = nai.generate_img2img(
                job.workflow, i2i["image"], strength=i2i.get("strength"),
                width=i2i.get("width", nai.NAI_WIDTH),
                height=i2i.get("height", nai.NAI_HEIGHT))
        else:
            # 文生图看渠道：`nai_wide` 出横版 1216×832；入队时快照说本轮原话
            # 带「横屏 / 横版 / 横图」也走横版（用户口径：「说 nai 横屏」=
            # nai_wide，见上面那段注释）。**读快照不现读原话**——worker 线程
            # 里读不到，见 Job.landscape。
            png = nai.generate(
                job.workflow,
                wide=(job.skill == "nai_wide" or job.landscape))
    except Exception as exc:
        job.error = exc
        log.warning("NAI 生图失败 %s %s：%s", job.target, job.target_id, exc)
        _notice(job)
        return
    job.skill_done = True
    if job.target is None:
        # NAI 仅限 QQ 群，正常走不到这里（generate_image 已挡 web）；保险起见
        # 仍把图落盘，网页侧可下载。job.entry 直接放文件路径。
        from app import image_out
        job.entry = {"images": [image_out.save_bytes(png, "png", "nai")]}
        return
    try:
        from app import image_audit, image_out
        path = image_out.save_bytes(png, "png", "nai")
        # 审核闸门。拦下时**不设 job.error**：图确实出出来了，只是没过审，
        # 而且 image_audit 已经回过一句提示了，再报错是重复。
        if not image_audit.allow_send(
                path, QQ_AGENT_ID, job.target, job.target_id,
                meta={"tag": job.tag, "skill": job.skill or "", "seed": None,
                      "caption": "", "file": "",
                      "prompt": job.workflow or ""}):
            log.info("NAI 生图被审核拦下，未发回 %s %s",
                     job.target, job.target_id)
            return
        _send_image_bytes(job.target, job.target_id, path)
        log.info("NAI 生图完成已发回 %s %s（耗时 %.1f 秒）",
                 job.target, job.target_id, _elapsed(job))
    except Exception as exc:
        job.error = exc
        _notice(job, stage="send")


def process(job):
    """跑一个任务：提交 → 限时等出图 → 发回原会话 / 存给网页侧。

    独立成函数是为了能同步调用（测试直接调它，不依赖真线程）。
    """
    job.started = time.time()       # 排队到此为止，后面都算「画这张用了多久」
    # NAI 云端生图：完全不碰 ComfyUI（token 是群主独立的，图由 NovelAI 出）。
    if job.skill in NAI_SKILLS:
        return _process_nai(job)
    # 放在换渠道 /free 之前：重启成功后 _COMFY.last_skill 会被清成 None（新进程里
    # 一个模型都没加载），换渠道那条就不会再打一次没用的 /free。
    _maybe_restart_for_clean_start(job)
    _maybe_release_for_switch(job)
    _maybe_release_for_low_vram()
    try:
        # 横屏：入队时快照的 `job.landscape`（见 Job.landscape 那段）。放在
        # `_queue_prompt` 之前、而不是 `_generate_image` 里，是为了连静默重抽
        # 那条「重建 job.workflow 再递归调 process」的路径也一起覆盖——
        # 快照存在 job 上，重抽几次都对调得一样。
        wf = (landscape_workflow(job.workflow) if job.landscape
              else job.workflow)
        job.prompt_id = _queue_prompt(wf)
    except Exception as exc:
        job.error = exc
        _notice(job)
        return

    try:
        entry = wait_done(job.prompt_id, task_timeout(job))
    except TimeoutError as exc:
        # 关键：中断 + 从队列摘掉 + 释放显存。串行队列下「当前正在执行」的
        # 就是我们这一张，所以 /interrupt 是准的——这也是改成全局串行之后
        # 才敢用它（从前不知道在跑的是不是自己的图，只能干等）。
        _abort(job.prompt_id)
        # /interrupt 是异步的：ComfyUI 要等当前节点跑完这一步才真正停下，
        # 这期间提交的新任务只会被排进它的 pending。在这里等到 running 清空、
        # 显存真的腾出来，才把下一个任务交出去——不靠盲等固定秒数。
        idle = _wait_comfy_idle()
        # 超时有两种：画得慢，或者 ComfyUI 中途没了。探一下就能分辨（09-30
        # 实测：ComfyUI 01:19:41 崩了，01:24:00 那张却报「画超时了…麻烦重新
        # 生成一次」，对方照着重试也不会成）。探活放在 _wait_comfy_idle 之后
        # ——那张图的中断/清理先跑完，之后进程还在不在才是真信号。
        alive = _comfy_up()
        if alive:
            # 只要 ComfyUI 还活着就清一次——两种成因都算数：
            # ① **退不了场**（not idle）：卡在一次不返回的 CUDA 调用里
            #    （TDR / 僵尸），`/interrupt` 设的协作标志它永远读不到，
            #    后面每一张都会撞上同一个忙队列、逐张超时；
            # ② **退场了、但这张烧满了时限**（idle）：说明机器
            #    状态已经不对了。2026-10-03 实测的级联：03:05:09 超时（内存
            #    还有 6.3GB）之后，03:08:19 紧接着又超时（内存 0.1GB）——
            #    第一张慢死，第二张接着死。清一次比继续往下塞划算。
            # 两种情况 ComfyUI 都**不会自己好**。
            # ⚠️ 真挂了（not alive）**不能**重启：`_restart_comfy` 会白等满
            # COMFY_RESTART_WAIT(180s) 才放弃，而下一张本来也只会撞上
            # 「ComfyUI 没在线」——那句话比这里诚实。
            why = ("被中断后 %.0f 秒仍未退场（活着但卡住）" % COMFY_IDLE_WAIT
                   if not idle else
                   "退场了，但这张烧满了 %.0f 秒（状态已不对）" % task_timeout(job))
            log.warning("ComfyUI %s，重启它", why)
            _restart_comfy()
            alive = _comfy_up()      # 重启成没成，以重启后再探一次为准
        job.error = exc if alive else ComfyGone()
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
        # audit_silent（随机口令）→ _send_image 里审核拦下时**不回话**，
        # 话术由这里统一管：重抽期间完全静默，重抽尽才回一句软话术。
        silent = getattr(job, "audit_silent", False)
        # notify 只在静默 job 上降级——普通 job 的调用形态与从前完全一致
        # （worker 层有别的 _send_image 替身按老签名接，别无条件加参）。
        sent = sum(1 for name in names
                   if _send_image(job.target, job.target_id, name, job.tag,
                                  skill=job.skill or "", seed=job.seed,
                                  prompt=job.prompt or "",
                                  **({"notify": False} if silent else {})))
        if sent:
            log.info("生图完成已发回 %s %s：%d/%d 张（编号 %s，渠道 %s，"
                     "seed %s，耗时 %.1f 秒）",
                     job.target, job.target_id, sent, len(names), job.tag,
                     job.skill or "默认", job.seed, _elapsed(job))
            # 记进账本：编号 → 提示词 + 种子。**只在真发出去之后记**——没发出去的图
            # 不该有编号可查，否则查出来一句提示词、对方手上却没有那张图。
            # 落盘失败也不该影响发图（image_log.save 自己吞掉异常）。
            # seed 存的是**这一个数**：动漫渠道两段采样共用它（见
            # generate_image 里 __SEED__ 的全局替换），查回来就够复现。
            image_log.save(job.tag, prompt=job.prompt, file=names[0],
                           skill=job.skill or "", target=job.target,
                           target_id=job.target_id,
                           seed="" if job.seed is None else job.seed)
        elif silent and getattr(job, "resample_fn", None) and job.retry_left > 0:
            # 随机口令被拦 → **静默重抽**（2026-10-05 用户拍板）：图是机器人
            # 自己推的服务，不是用户点的单，回「未过审」没道理。换一条新
            # 提示词、掷新种子，同 job 重跑——不重新入队（不再扣额度、不占
            # 新的并发槽，worker 线程内串行重跑没有死锁风险）。tag 保持不变。
            job.retry_left -= 1
            new_prompt = job.resample_fn()
            if new_prompt:
                from app.tools.normal import generate_image as gi
                new_seed = random.randint(0, 2 ** 31 - 1)
                wf = gi.build_t2i_workflow(job.skill, new_prompt, new_seed)
                if wf is not None:
                    log.info("随机图被审核拦下，静默重抽（剩 %d 次）%s %s",
                             job.retry_left, job.target, job.target_id)
                    job.workflow = wf
                    job.prompt = new_prompt
                    job.seed = new_seed
                    job.error = None
                    job.started = time.time()
                    return process(job)
            # 重抽拿不出新提示词 / 模板没了 → 落到下面的兜底话术
            _notify_random_blocked(job)
        elif silent:
            # 重抽已尽（或拿不出新提示词）：回一句软话术，**不提审核**——
            # 用户视角这只是机器人自己出的图没画好。
            _notify_random_blocked(job)
        else:
            # 全被审核拦下了。**不当失败处理**：图确实画出来了、也通知过对方了
            # （image_audit 自己回的那句提示），再走 _notice 就是重复报错，
            # 而且会让额度退回去——等于给了一条「靠生成违规图刷额度」的路。
            log.info("生图全部被审核拦下，未发回 %s %s（%d 张）",
                     job.target, job.target_id, len(names))
    except Exception as exc:
        job.error = exc
        _notice(job, stage="send")


def _finish(job):
    """还名额、把任务移出 running、唤醒等结果的网页侧。失败路径也一定要走到。

    两条通道共用这一份：通道从任务自己身上取（见 _channel_of_job），所以
    `_take` 那边不用把 chan 一层层传下来。
    """
    chan = _channel_of_job(job)
    refund = False
    with _lock:
        key = _key(job.target, job.target_id)
        n = chan.per_session.get(key, 0) - 1
        if n > 0:
            chan.per_session[key] = n
        else:
            chan.per_session.pop(key, None)
        try:
            chan.running.remove(job)
        except ValueError:
            pass                        # 没在跑（测试手搓的 Job）——不是错误
        # 只有**真出图了**的重渠道才开冷却窗。失败/超时那张已经把 ComfyUI 的
        # 队列和显存清干净了（_abort + _wait_comfy_idle），没有残留要等它散，
        # 再罚它 90 秒只是白等。云端通道不参与：它没有显存要等（见 _next_ready）。
        if chan.local and job.weight > 1 and job.skill_done:
            chan.heavy_done_at = time.time()
            log.info("重渠道 %s 跑完，%.0f 秒内不再接重活",
                     job.skill, QWEN_COOLDOWN)
        # 记一条回执：模型在 enqueue 拿到「已经排上队了」之后就**再也收不到
        # 任何消息**，全靠这条知道上一张到底出没出图（见 recent_line）。
        # ok 的判据是「真出图了」且投递没出错——图没发回会话，对群友就等于没画。
        ok = bool(job.skill_done) and job.error is None
        _recent.append({
            "target": job.target,
            "target_id": job.target_id,
            "skill": job.skill or "",
            "ok": ok,
            "err": (_reason(job.error) if job.error else ""),
            "ts": time.time(),
        })
        # 私聊每日额度：接单时扣过了，但**这张没出图**就得退回去——额度管的是
        # 「你能拿到几张图」，不是「你能让我们失败几次」。判据跟上面的 ok 同源，
        # 不另立一套，免得出现「回执说失败、额度却照扣」这种自相矛盾的状态。
        refund = job.quota_charged and not ok
    if refund:
        # 放在锁外：退款要落盘，不该占着队列锁做磁盘 IO。
        from app import image_quota
        left = image_quota.refund(job.target_id)
        log.info("这张没出图，退还私聊额度：%s 今日已用 %d 张",
                 job.target_id, left)
    job.done.set()


def _drain():
    """把**两条通道**里的任务同步跑完，不依赖 worker 线程（测试用）。

    有了它，测试就能像从前那样「调用 → 立刻断言提交了什么」，不必陪真线程
    玩时序。本地通道优先取：拆通道之前只有一条队列，测试里本地任务先入队、
    就该先跑——先取本地能让那批老用例的先后关系原样保留。
    """
    while True:
        job = _take_nowait(_COMFY) or _take_nowait(_NAI)
        if job is None:
            return
        try:
            process(job)
        finally:
            _finish(job)


def _reset():
    """清空两条通道的队列与计数（测试用）。不动 worker_started——测试自己把
    _ensure_worker mock 成空操作，真线程不该被这里牵起来。"""
    with _lock:
        for chan in (_COMFY, _NAI):
            chan.queue.clear()
            chan.per_session.clear()
            chan.running = []
            chan.seq = 0
            chan.heavy_done_at = 0.0
            chan.last_skill = None
            chan.same_run = 0
            chan.last_restart_try = 0.0
        _recent.clear()


# ─── ComfyUI 交互 ────────────────────────────────────

class ComfyGone(RuntimeError):
    """等图期间 ComfyUI 掉线了——**不是**「画得慢」。

    为什么要单独一个类型：超时有两种，话术必须分开（2026-09-30 实测）。
    01:19:41 ComfyUI 原生崩溃（faulthandler 只有 C 栈），而 01:24:00 那张报给
    用户的却是「画超时了（超过 180 秒没出图），麻烦重新生成一次」——把「服务
    没了」说成「画得慢」，对方会一直重试，而重试一次也不会成。探一下就能分辨，
    不该让用户去猜。
    """

    def __str__(self):
        return "ComfyUI 掉线"


def _comfy_up(timeout=3):
    """ComfyUI 还在不在——只探，不写日志。

    和 comfy_alive 分开，是因为那条日志写着「本次不入队」，而这里是在**等图
    期间**探的，「不入队」这句话放这儿是错的。
    """
    try:
        requests.get(COMFYUI_URL + "/system_stats",
                     timeout=timeout).raise_for_status()
        return True
    except Exception:
        return False


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


# ── 横屏（2026-10-07）────────────────────────────────────────
# 用户口径：「到时候我们就说，silver，横屏，这样就可以触发了」。所以判据就是
# **本轮原话里有没有那三个词**——与 `generate_image._hd_tier_guard` /
# `_t2i_guard` 同一个证据源（`qq_api.current_turn_text`，理由见那边的长注释）。
#
# ⚠️ 但这个证据源**只能在入队线程读**：`_hd_tier_guard` 是在工具里同步判的
#    （会话线程，上下文还绑着），而这里要落到 worker 线程执行。所以判据在
#    `enqueue()` 里算一次、存进 `job.landscape`，`process()` 只读快照。
#    2026-10-07 第一版就是直接在 `process()` 里读 `current_turn_text()`，
#    单测（同线程调 process）全绿、生产必挂——worker 读出来恒为 None。
#    教训：**「跟某某守卫同源」不等于「可以在同一个位置读」**，先问这行代码
#    跑在哪个线程。
#
# 落地 = 把工作流里**那个尺寸节点的宽高对调**。为什么不像 `nai_wide` 那样每个
# 渠道存一份横版工作流：本机 13 个渠道的尺寸节点 id 各不相同（8/9/15/23/30/53），
# class_type 也有三种，逐个复制要手工维护 13 份；对调只需认「哪个节点带
# width+height」这一条，而且以后新增渠道自动生效。
# 后面几级放大全是**倍率**（`LatentUpscaleBy` / `ImageUpscaleWithModel`），
# 所以只改这一处，最终尺寸自动跟着翻。
_LANDSCAPE_RE = re.compile(r"(横屏|横版|横图)")
# 带画布的节点类型（实测这三种）。**按 class_type 认、不按节点 id**——id 各渠道不同。
_SIZE_NODE_TYPES = ("EmptyLatentImage", "EmptySD3LatentImage",
                    "BatchPromptImageGenerator")


def turn_is_landscape():
    """本轮原话里有没有「横屏 / 横版 / 横图」。

    拿不到原话（网页端 / 单测直接调工具）→ False，即**不改尺寸**。跟
    `_hd_tier_guard` 一样：没有证据源就不替调用方猜。

    ⚠️ **只能在会话线程调**（`enqueue` 里那一处）。worker 线程读出来是 None。
    """
    from app import qq_api
    text = qq_api.current_turn_text()
    return bool(text) and bool(_LANDSCAPE_RE.search(text))


def _find_size_node(workflow):
    """找那个「带画布」的节点 id；找不到返回 None。"""
    for nid, node in workflow.items():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") not in _SIZE_NODE_TYPES:
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict) and "width" in inputs and "height" in inputs:
            return nid
    return None


def landscape_workflow(workflow):
    """把尺寸节点的宽高对调，返回**新的** workflow（不改原对象）。

    找不到尺寸节点 / 宽高不是数 → 原样返回（绝不因为这点事把图卡住）。
    返回新对象而不是原地改：静默重抽那条分支会重建 `job.workflow` 再递归调
    `process`，原地改会被对调两次（等于没改）。

    `BatchPromptImageGenerator` 还带一组 `hires_width` / `hires_height`
    （`enable_hires` 默认 false），一并换掉——不然哪天开了它，放大那级还是竖的。
    """
    nid = _find_size_node(workflow)
    if nid is None:
        return workflow
    node = workflow[nid]
    inputs = node["inputs"]
    w, h = inputs.get("width"), inputs.get("height")
    if not (isinstance(w, int) and isinstance(h, int)):
        return workflow
    new_inputs = dict(inputs)
    new_inputs["width"], new_inputs["height"] = h, w
    if "hires_width" in new_inputs and "hires_height" in new_inputs:
        new_inputs["hires_width"], new_inputs["hires_height"] = (
            new_inputs["hires_height"], new_inputs["hires_width"])
    new_node = dict(node)
    new_node["inputs"] = new_inputs
    out = dict(workflow)
    out[nid] = new_node
    return out


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


def _wait_comfy_idle(timeout=COMFY_IDLE_WAIT):
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


def _fail_text(exc, stage="submit", skill=None):
    """给对方看的失败说明。

    超时单独给一句——它最常见，而且对方重画一次就好（重画时模型已经加载在
    显存里，通常几秒到几十秒就出）。连不上 ComfyUI 也只给一句人话，不把
    半截 HTTPConnectionPool 甩出去。

    stage="send" 是投递阶段挂的（图都画好了，是发回会话那步失败），跟
    ComfyUI 在不在没关系，别往它头上安。

    skill 是 NAI 那两个渠道（`nai` / `nai_wide`）时走云端 NovelAI，失败是网络 /
    代理问题，跟 ComfyUI 完全无关——绝不把「ComfyUI 没在线」甩给群友看。
    """
    if stage == "send":
        # 走到这儿**图一定已经画出来了**（`_notice(stage="send")` 只在
        # `job.skill_done = True` 之后的投递步骤被调用），所以绝不能再说
        # 「图没画出来」——那是在撒谎，而且会把对方引向「重画一次」这个
        # 完全没用的方向。
        # 2026-10-01 03:44 就是这么坑了胡桃桃：Anima_00276_.png 明明出图了
        # （3.89MB），它收到的却是「图没画出来（OneBot 调用失败 send_private_msg: ）」。
        # 根因在 qq_api（QQ 换了种措辞、没被认出来，兜底链路没启动），但
        # 「说图没画出来」这句谎话是这里说的。
        from app import qq_api
        if qq_api.friend_required_error(exc):
            return "图画好了，但私聊发不出去——得先加好友。加完再喊我一次。"
        return "图画好了，但没发出去（%s）。稍后再喊我一次。" % _reason(exc)
    if skill in NAI_SKILLS:
        if isinstance(exc, TimeoutError) or _is_unreachable(exc):
            return ("图没画出来——连不上 NovelAI 的服务器（多半是网络或代理问题）。"
                    "让对方稍后再试；一直连不上就让群主检查 NAI 的代理设置。")
        return "图没画出来（NAI：%s）" % _reason(exc)
    if isinstance(exc, ComfyGone):
        # 「掉线」和「超时」是两回事：前者重画也没用，得先把服务拉起来。
        return ("图没画出来——ComfyUI 中途掉线了（不是画得慢）。"
                "等它重新起来再让对方重画。")
    if isinstance(exc, TimeoutError):
        return ("画超时了（超过 %d 秒没出图），已经中断这张。"
                "麻烦重新生成一次。" % task_timeout(skill))
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
        _send_text(job.target, job.target_id, _fail_text(job.error, stage, job.skill))
    except Exception as exc:
        # 对方不是好友时，连这句失败说明都发不出去（QQ 不允许给非好友发私聊）。
        # 那是同一条策略限制，别在这儿再留一段看着像崩溃的堆栈——qq_bot 那边
        # 已经有一条说得清的 WARNING 加一次管理员通知了。
        from app import qq_api
        if qq_api.friend_required_error(exc):
            log.warning("生图失败说明也发不出去 %s %s：对方还不是好友",
                        job.target, job.target_id)
        else:
            log.exception("生图失败说明也发不出去 %s %s", job.target, job.target_id)


def _send_text(target, target_id, text):
    from app import qq_api
    if target == "group":
        qq_api.send_group(target_id, text)
    else:
        qq_api.send_private(target_id, text)


# ─── 提交回执「后台直发」标记（2026-10-04 用户拍板）─────────────
#
# 用户原话：「AI 提交任务之后直接发一个回执，就说任务已经提交、前面还有 XX 在
# 排队；这个是**直接发的，不是经过 AI**——这个 AI 老是瞎编东西，我要的是最直接
# 的来自后台的回执。」所以提交成功那一刻由 `generate_image` 工具**自己**把回执
# 发进会话，模型不再转述（它一转述就编张数）。
#
# 工具返回给模型的那段以这个标记开头，QQ 适配层（qq_bot）看到它就知道：
#   1. 人话那半句已经发出去了，**本轮不要再采纳模型的正文**；
#   2. 冷却计时 / 群聊背景要按「机器人说过话」处理。
# 唯一的真相源就在这里：解析只用 `receipt_line()`，别在别处再写一遍格式。
RECEIPT_SENT_MARK = "【回执已直发】"


def receipt_line(result):
    """从工具回执里取出后台**已经直接发出**的那句话；不是直发回执返回 ""。

    直发回执长这样（第二行起才是写给模型的叮嘱）：

        【回执已直发】任务已提交，前面还有 1 张在排队。
        （系统已经把上面那句直接发到会话里了…）

    网页端、失败退回、被闸拦下的回执都没有这个标记，一律返回 ""——
    调用方据此区分「后台已经回话了」和「还得让模型开口」。
    """
    text = str(result or "")
    if not text.startswith(RECEIPT_SENT_MARK):
        return ""
    body = text[len(RECEIPT_SENT_MARK):]
    return body.split("\n", 1)[0].strip()


def _elapsed(job):
    """这张图从**真正开跑**到此刻的秒数（不含排队）。

    started 没打上（异常路径、测试手搓的 Job）就退回入队时刻——宁可把排队
    时间算进去，也别在日志里报个负数或 None。
    """
    return max(0.0, time.time() - (job.started or job.created))


def _caption(tag, path, skill, image_out, seed=None):
    """图上那行字：`编号 · 分辨率 · 渠道 · seed 数字`。

    编号**必须在最前且原样**——它是群友引用那条消息时被正则抠回来的锚点
    （见 app/image_log.py，TAG_RE 只认 `HT-` 开头那一串）。后面几项纯粹是给
    人看的附注，读不出来就少一项，不影响编号。

    为什么要在这行里带 seed（2026-10-02 用户提，需求是「图有多手多脚，拿种子
    改提示词重画」）：caption 跟图片在**同一条消息**里，群友引用它时整行正文
    回到模型眼前——种子于是自己就回来了，不用模型先猜编号再去查账本。这是
    唯一一条不依赖模型记忆的通路（理由同 image_log 模块开头那段）。

    没种子时（NAI 那种云端图 / 老调用方）不多写那一段，整行与从前一致。

    ⚠️ 每一项都只放**字母数字和 `-`**：这行会过一遍 `qq_api.to_qq_text` 做
    Markdown 降级，`_` `*` 反引号 那些会被吃掉或改变排版。
    """
    if not tag:
        return ""
    bits = [str(tag)]
    size = image_out.local_size(path)
    if size:
        bits.append(size)
    if skill:
        bits.append(str(skill))
    if seed is not None:
        bits.append("seed %d" % int(seed))
    line = " · ".join(bits)
    # 后面那段附言**可在管理页改**（settings.json 的 `caption_note`，见
    # agents.caption_note）。2026-10-06 用户拍板：「那些字我就希望我可以自己
    # 去附加这一些内容……一点换行、二点换行」。没设就用内置默认（引用这张图
    # 的三条用法——他实测下来「引用图一直出问题」的就是这三条没讲清）。
    # 附言只在有编号时跟着走：`_caption` 没 tag 就整行不发，附言也不该单独刷一条。
    note = _caption_note_text()
    return line + ("\n" + note if note else "")


# 出图 caption 附言的内置默认（管理页整段可改；把框**清空** = 这段不要了，
# 只发 `编号 · 分辨率 · 渠道 · seed` 那一行）。
# 三条各占一行——用户要的正是「一点换行、二点换行」。
_CAPTION_NOTE_DEFAULT = (
    "1、引用这张图 +「提取提示词」= 把这张当初用的提示词发给你\n"
    "2、引用这张图 + 说需求 = 照这张图改提示词，用同一渠道重画\n"
    "3、要只改画面里那一处 = 引用这张图 +「qwen 图生图 + 怎么改」（慢，1~2 分钟）"
)


def _caption_note_text():
    """这段附言的当前内容：管理页设了就用设的（空串 = 不要附言），没设用默认。"""
    try:
        from app.agents import caption_note
        note = caption_note(QQ_AGENT_ID)
    except Exception:                        # 配置读坏了也不能把发图这一路弄断
        return _CAPTION_NOTE_DEFAULT
    return _CAPTION_NOTE_DEFAULT if note is None else note


def caption_note_default():
    """内置的那份附言（管理页 GET 用它当编辑框的初始内容）。"""
    return _CAPTION_NOTE_DEFAULT


# 随机口令重抽尽后的兜底话术（image_audit 的 BLOCKED_NOTICE/FAILED_NOTICE
# 都不合适：那是「用户点的单被拦」的话术，随机图是机器人自己推的服务）。
RANDOM_BLOCKED_NOTICE = "这轮抽到的画面没画好，再发一次口令试试～"


def _notify_random_blocked(job):
    """随机口令重抽尽的兜底话术。**绝不提审核**——用户视角这只是机器人
    自己出的图没画好，提「审核」反而引人去故意试探边界。发不出去就算了，
    不抛异常（跟 _notify_blocked 同一纪律）。"""
    from app import qq_api
    try:
        if job.target == "group":
            qq_api.send_group(job.target_id, RANDOM_BLOCKED_NOTICE)
        else:
            qq_api.send_private(job.target_id, RANDOM_BLOCKED_NOTICE)
    except Exception as exc:
        log.warning("随机图兜底话术发送失败 %s %s：%s",
                    job.target, job.target_id, exc)


def _send_image(target, target_id, filename, tag="", skill="", seed=None,
                notify=True, prompt=""):
    """发回原会话。先过 image_out 甩掉 PNG 里的工作流元数据，编码格式看管理页开关。

    返回 True = 真发出去了。审核拦下时返回 False（**不抛异常**）——
    调用方靠它区分「发了」和「被拦了」，别把拦截记成发送失败。

    tag / skill / seed 是这张图的编号、渠道和种子，作为 caption 和图片放在
    **同一条消息**里（见 app/image_log.py 与本文件 `_caption`）：群友引用这条
    消息时它们会跟着引用回到模型眼前。不传 tag = 不加那行字，行为与从前一致；
    不传 seed = 那一段不写。

    prompt（2026-10-06）只为**被拦时**服务：它和上面几项一起进人工二审队列，
    管理员点「过审」补发时，图有了、账本也补得上（编号 → 提示词）。正常发送
    路径不用它（账本由调用方在确认发出后记）。

    ⚠️ 审核审的是 `prepare_for_send` 的产物（本地文件），也就是**真正要发出去
    的那份字节**，不是 ComfyUI 的原图。见 app/image_audit.py 的模块注释。
    """
    from app import image_audit, image_out, qq_api
    from app.agents import image_send_format
    fmt = image_send_format(QQ_AGENT_ID, target, target_id)
    path = image_out.prepare_for_send(filename, fmt)
    # caption 提到审核**之前**算：被拦时它要跟着图一起进人工二审队列，管理员
    # 点「过审」补发的就是这一行原文——跟没被拦过的图一个字都不差。
    caption = _caption(tag, path, skill, image_out, seed)
    if not image_audit.allow_send(path, QQ_AGENT_ID, target, target_id,
                                  notify=notify,
                                  meta={"tag": tag, "skill": skill,
                                        "seed": seed, "caption": caption,
                                        "file": filename, "prompt": prompt}):
        return False
    qq_api.send_image(target, target_id, path, caption=caption)
    return True
