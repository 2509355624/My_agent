"""手动跑图台（页面 /gen）：没有 AI 的文生图界面。

和漫画工作台的分工：那边是「AI 写剧本 → 渲染」，这边是「人写提示词 → 直接跑」。
渠道清单、提交链路、队列守卫全部复用现成件（见各函数注释），本模块只做
「接住表单 → 起线程 → 记账 → 供图」这一层。

## 存储约定（2026-10-14 用户拍板：「历史栏就是 output 文件夹不是吗」）

- **成图不复制**：ComfyUI 的 output 目录就是所有图的归宿（QQ 机器人跑的也在
  那）。`/api/gen/img` **先读本机磁盘、读不到才代理 `/view`**（2026-11-10：
  RAM 守卫重启 ComfyUI 的空窗里 `/view` 全 404，图明明跑好了却是永久破图）。
  本机不留第二份字节——单张好几 MB，复制一本历史就是几百 MB 的重复。
- **账本只记元数据**：`state/gen_pad.jsonl`，一行一张图（tag / 渠道 / 提示词 /
  种子 / 图片引用 / 时间 / 成败）。ComfyUI 图会随 output 目录清理而消失；NAI
  图保存在 `image_out` 的临时目录，按同一套过期清理规则处理。账本不会删除——
  取不到图时历史项显示为破图，不假装有图。
- ⚠️ 为什么不复用 `image_log`：那个账本**只在图真发到 QQ 会话之后才写**
  （`image_jobs.process` 里 `job.target is None` 直接 return），网页侧提交的图
  （漫画格 / 写真 / 本台）它一行都不记。想让那边记就得改 QQ 关键路径，不干。
- 「清空历史」= 记一个截止时间戳（`state/gen_pad_cleared_at`），早于它的不显示。
  文件和账本行都不删——它是 output 文件夹的索引，不是垃圾篓。

## NAI 云端渠道

`nai` / `nai_wide` 没有 ComfyUI workflow，而是复用 `image_jobs` 的独立 NAI
云端队列。网页侧任务使用 `target=None`，worker 会把返回的 PNG 落到
`image_out` 临时目录；跑图台只保存带 `nai:` 前缀的受控文件名，不保存绝对路径。
"""

import base64
import json
import logging
import os
import random
import re
import threading
import time

import requests
from flask import (Blueprint, Response, jsonify, request, send_file,
                   send_from_directory)

from app.features.image import comfy_out
from app.features.image import lowres
from app.features.image import lora_slots
from app.features.qq import api as qq_api
from app.features.comic import comic
from app.features.image import jobs as image_jobs
from app.features.image import out as image_out
from app.features.image import gen_steps
from app.features.image import gen_params
from app.core.cancel import Cancelled
from app.config import COMFYUI_URL, NAI_API_KEY, WEB_DIR, state_path
from app.config import QQ_AGENT_ID, WORKSPACE_LOWRES
from app.features.image.direct_gen import _allowed_skills
from app.gateways import visitor as _visitor
from app.core.skills import load_skill, skill_summary

log = logging.getLogger("gen_api")

bp = Blueprint("gen", __name__)

# 取图专用 session：本地 ComfyUI，走系统代理只会绕远路（和 comic 侧同规矩）。
_session = requests.Session()
_session.trust_env = False

# ─── 进行中的任务（进程内，重启即空） ─────────────────────
#
# 为什么不在账本里轮：排队/在画是瞬时状态，落盘只会多一份要清理的中间态；
# 刷新页面丢掉进行中任务是可接受的（图还在队列里跑，账本里会出现）。
_JOBS = {}
_JOBS_LOCK = threading.Lock()
# 跑图台按用户限流，限额复用 image_jobs.inflight_limit()。
_JOB_KEEP = 40           # 内存里最多留几个任务记录
_LORA_SAVE_LOCK = threading.Lock()   # 「存为默认」是改模板文件，串行化
# 提示词长度上限（2026-10-30 由 4000 提到 10 万）：这个台是给长稿用的——
# 跑 moss 小漫画的分镜提示词动辄几千字，4000 会直接吃 400 把稿子挡在外面。
# 原来那句「防手滑贴进一整篇小说」的顾虑交给用户自己判断，代码不再替他砍。
_MAX_PROMPT = 100000
# 一次最多认几张参考图。每张都要走一次识图（几秒 + 一份额度），挂十张等于
# 让用户干等半分钟；超过的部分直接忽略，不报错。
MAX_REFS = 3
# `/api/gen/adopt` 认领任务的时间窗（秒）。记录 id 里的时间戳是毫秒精度的
# 出生时间，用户那条记录就是那一刻建的，正常差不到 1 秒；给 2 分钟是留给
# 「本机时钟比服务器快一点」「提交接口排队」这类噪声，不能给更大——再宽就
# 会把同一句话前后两分钟的两次提交认成同一条。
ADOPT_WINDOW = 120.0

# 账本与「清空截止」（测试进程经 state_path 自动落临时目录，互不污染）。
_LEDGER = state_path("gen_pad.jsonl")
_CLEARED_AT = state_path("gen_pad_cleared_at")
_ledger_lock = threading.Lock()

# ComfyUI 输出文件名：纯文件名（output 目录是平的），挡死一切路径形态。
_IMG_NAME_RE = re.compile(r"[A-Za-z0-9._\- ]{1,120}")

# 出图缓存：同一个文件名内容永远不会变（ComfyUI 不会覆盖同名），所以可以给
# 一年 + immutable，浏览器发 If-None-Match 时还能拿 304。见 api_img 的说明。
_IMG_MAX_AGE = 31536000
_IMG_CACHE_CTL = "private, max-age=%d, immutable" % _IMG_MAX_AGE
_NAI_REF_RE = re.compile(r"nai:([A-Za-z0-9._\- ]{1,120})")
_NAI_PREFIX_MAX = 2000

# 缩略图（2026-11-24）：出图原图 2~8MB，聊天区铺十几张就是几十 MB 全走
# 家用上行。`/api/gen/img?w=640` 走这里。允许的宽度白名单——不给任意值
# 是为了让磁盘缓存命中，也免了拿它当任意分辨率转换器。
# 真正干活的是 `gallery.thumb`（WebP + 按路径哈希缓存 + 每文件一把锁 +
# mtime 校验），这边不重写那一套，只做「参数白名单」和「找不到就退回原图」。
_IMG_THUMB_W = (240, 480, 640, 900)

# 「能存下来看」的那一档。2026-11-24 用户先要 512k，当天晚些又改口：
# 「我感觉现在只有 102k 还是太小了……直接压到 1m 即可，不管什么图片，只要是
# 工作台的，服务端压缩到 1m 再传过来，点击图片不需要重新传 10m 的原图」。
#
# 为什么不是再列几个宽度：宽度和体积对不上——1280 的复杂插画 WebP 能到 600KB，
# 而 1024 的平涂只有 200KB。用户要的是一个**体积上限**，所以这一档单独走：
# 长边**从大往小试**，取第一张不超过 cap 的；都超了就给最小的那张（再缩就糊了）。
# 从大往小是关键：反过来试的话，第一张 768 的 WebP 往往才 50KB 就「合格」了，
# 于是永远出不来高清图——上一版「只有 102k」就是这么来的。
# 缓存文件名带宽度（`w1536`），改了上限自然换档，不会读到旧缓存。
_IMG_FIT_W = (1536, 1280, 1024, 768)
_IMG_FIT_CAP = 1024 * 1024
# 只认这一个值；以前写 `512k` 的旧链接继续按同一个上限发，免得碎图。
_IMG_FIT_KEYS = ("1m", "1024k", "1024", "512k", "512")


def _img_thumb_w(raw):
    """解析 `?w=`：只认白名单里的宽度，其余一律当「不要缩略图」。"""
    try:
        w = int(str(raw or "0").strip())
    except (TypeError, ValueError):
        return 0
    return w if w in _IMG_THUMB_W else 0


def _img_fit(raw):
    """`?fit=1m` → 字节上限；不认识的值返回 0（发原图）。"""
    if str(raw or "").strip().lower() in _IMG_FIT_KEYS:
        return _IMG_FIT_CAP
    return 0


def _img_fit_file(src, cap):
    """长边从大往小试，取第一张 ≤ cap 的 WebP。都超了就给最小的那张。

    缓存直接落在 `gallery.cache_path` 定好的位置（`state/gallery_cache/` 同一
    套目录、同一把锁、同样的 mtime 校验），不另开一套缓存——只是「选哪一档
    宽度」这件事 gallery.thumb 做不了：它按宽度缩一次就完事。

    原图本身长边不到最宽那档时（角色扮演的竖图多是 832×1216），thumb 只会
    等比缩小，所以第一档就等于原分辨率；超过 cap 才继续往下退。
    """
    try:
        from app.features.gallery import gallery as _gallery
        best = None
        for w in _IMG_FIT_W:
            p = _gallery.thumb(src, edge_override=w)
            if not p:
                return None
            best = p
            try:
                if os.path.getsize(p) <= cap:
                    return p
            except OSError:
                return None
        return best
    except Exception:                       # noqa: BLE001
        log.debug("1M 档生成失败 %s", src, exc_info=True)
        return None


def _img_thumb_file(src, w):
    """原图的 w 长边缩略图；生成失败返回 None（调用方退回原图）。

    缩略图只是优化，不是前置条件：Pillow 不在、图坏了、路径越界，一律
    返回 None 而不是抛异常——出图本身不能因为缩不出来就 500。
    """
    try:
        from app.features.gallery import gallery as _gallery
        return _gallery.thumb(src, edge_override=w)
    except Exception:                       # noqa: BLE001
        log.debug("缩略图生成失败 %s w=%s", src, w, exc_info=True)
        return None


def _img_send(path):
    """发一张缩略图 / 压缩档（WebP + 长缓存 + 可协商 304）。

    mimetype 必须是 `image/webp`：gallery.thumb 产的是 WebP，标成 jpeg 会让
    浏览器按 JPEG 解析然后裂图。
    """
    resp = send_file(path, mimetype="image/webp",
                     conditional=True, max_age=_IMG_MAX_AGE)
    resp.headers["Cache-Control"] = _IMG_CACHE_CTL
    return resp


def _nai_prompt(prompt, prefix):
    """把跑图台的 NAI 画师串放在主体提示词前；其它渠道不经过这里。"""
    prompt = str(prompt or "").strip()
    prefix = str(prefix or "").strip()
    if not prefix:
        return prompt
    return prefix.rstrip(",，;； \\n") + ", " + prompt


# ─── 中文提示词翻译（2026-11-23 修：手机端出图和描述完全对不上）──────
#
# 来路：新界面把用户在输入框里写的中文**原样** POST 给 `/api/gen/submit`，
# 这条接口直通 ComfyUI，不经过 QQ 那条 `direct_gen._translate`。于是
# `一位少女单人，留着黑色长发，戴着宽檐编织草帽` 被原样拼进英文 tag 串，
# 本地 CLIP 读不懂中文 —— 相当于「没给提示词」，出什么全靠模板里的通用
# 后缀，用户看到的就是「跟提示词完全不一致」。
#
# 分工：只翻译**读不懂中文的渠道**。qwen / moss 本来就能吃中文，翻过去反而
# 丢信息，还白烧一次 token，所以放行。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 这些渠道原生认中文（qwen 系、moss 系），不用翻。
_CN_NATIVE_PREFIX = ("qwen", "moss")
_TRANSLATE_TIMEOUT = 30


def _needs_translate(skill, prompt):
    """要不要翻：含中文 + 渠道不是原生认中文的那几个。"""
    if not _CJK_RE.search(prompt or ""):
        return False
    return not str(skill or "").startswith(_CN_NATIVE_PREFIX)


def _translate_prompt(skill, prompt):
    """中文需求 → 该渠道认的英文写法。失败返回空串，调用方保留原文。

    **不做硬编码词表**：只让模型转译，转成什么由它定。渠道写法按
    `direct_gen` 那套口径——标签渠道给逗号分隔的英文 tag，qwen 给句子。
    """
    from app.integrations import llm as llm_mod
    want = ("a comma-separated English danbooru tag string in channel `%s`'s "
            "style" % skill)
    content = (
        "把下面这段中文画面需求转译成英文生图提示词，只要成品本身，"
        "不要解释、不要引号、不要代码块。\n"
        "写法：%s。\n"
        "**只写画面内容**（人物、动作、服装、场景、构图、光线）。渠道的画风"
        "前缀和 LoRA 由系统自己挂，不要写画风词、不要写渠道名、不要写质量词"
        "（masterpiece / best quality 之类）。\n"
        "画面里要出现的文字（招牌、台词、LOGO）保留原文不翻。\n\n"
        "中文需求：\n%s" % (want, prompt))
    try:
        reply = llm_mod.call_llm([{"role": "user", "content": content}],
                                 timeout=_TRANSLATE_TIMEOUT)
    except Exception as e:                       # 翻不了就按原文跑，不堵队列
        log.warning("[gen] 中文提示词翻译失败：%s", e)
        return ""
    text = str(reply or "").strip()
    # 模型偶尔会套一层代码块或引号，剥掉；剥完还是中文说明没干活，按失败算。
    text = text.strip("`").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if len(text) >= 2 and text[0] in "\"'" and text[-1] == text[0]:
        text = text[1:-1].strip()
    if not text or _CJK_RE.search(text):
        log.warning("[gen] 翻译没拿到英文（%r）", text[:120])
        return ""
    return text


# ─── 渠道清单 ────────────────────────────────────────────

# 列表展示顺序：最常用的排前面，其余按名字。QQ 那边渠道词一堆，这里只挑
# 头部的几个——其余字母序也够找。
_HEAD = ("silver", "silver-hd", "hd_3_clear", "hd_3_curvy", "hd_3_gloss",
         "hd_3_soft", "anima-line", "anima-2s", "anima-real",
         "qwen_image_v1", "qwen-charsheet", "krea2-jiguang",
         "nai", "nai_wide", "cunny", "miao", "jank", "nffa")


def _size_editable(skill):
    """跑图台能不能自定义这个渠道的底图尺寸。

    **只开 moss 系**（`moss-qunyou` / `moss-light`，用户 2026-10-31 的原话是
    「我只针对跑图台的 moss 渠道」）：这两个渠道工作流里没有
    `LatentUpscaleBy` / `ImageUpscaleWithModel`，填多少就出多少。其余渠道
    带放大链，改底图会让最终像素和填写值对不上（silver 填 1024×1536 实际出
    2048×3072），不想让人误以为能精确控制。要放开哪个渠道，加在这里。
    """
    return str(skill or "").startswith("moss")


def channels():
    """跑图台可见渠道 = QQ 可点名渠道中的 ComfyUI 文生图渠道 + NAI 云端渠道。

    NAI 没有 `skills/<id>/workflow.json`，因此必须单独补元数据，不能让它走
    `load_skill()` 的本地工作流校验。
    """
    out = []
    try:
        allowed = _allowed_skills()
    except Exception:
        log.exception("渠道清单拉取失败（_allowed_skills）")
        return out
    for sk in allowed:
        if sk in image_jobs.NAI_SKILLS:
            if not NAI_API_KEY:
                continue
            out.append({"id": sk,
                        "summary": ("NovelAI 云端 · 竖版" if sk == "nai"
                                    else "NovelAI 云端 · 横版"),
                        "cloud": True})
            continue
        try:
            sd = load_skill(sk)
        except Exception:
            log.warning("渠道 %s 加载失败，跳过", sk, exc_info=True)
            continue
        if not sd or not sd.get("workflow"):
            continue                      # 没有文生图骨架的目录不是渠道
        out.append({"id": sk,
                    # 展示名（2026-10-29）：渠道 id 是目录名（ASCII），界面上
                    # 想显示中文时由 skill.md 的 `label` 提供。没有就用 id。
                    "label": sd.get("label") or sk,
                    "summary": skill_summary(sd.get("skill_md") or ""),
                    "cloud": False,
                    # 模板当前步数（"30" / "8+12"）：界面拿它当输入框 placeholder，
                    # 用户留空就是「按渠道默认」，不用每个渠道都写死一个数。
                    "steps": gen_steps.describe(sd.get("workflow") or {}),
                    # 同上，CFG 的模板当前值（"3" / "1+3"）。
                    "cfg": gen_params.describe_cfg(sd.get("workflow") or {}),
                    # 有没有可写的负向节点（qwen-charsheet 这类没有 CLIP 的
                    # 渠道为 False，界面把负向输入框藏起来）。
                    "negative": gen_params.has_negative(sd.get("workflow") or {}),
                    # 底图尺寸：模板当前值 + 能不能在跑图台改。
                    # **只有 moss 渠道开放**（用户 2026-10-31 明确要求「我只针对
                    # 跑图台的 moss 渠道」）——别的渠道带放大链，改底图会让最终
                    # 像素跟填写值对不上，不想让人误以为能精确控制。
                    "size": gen_params.describe_size(sd.get("workflow") or {}),
                    "size_editable": _size_editable(sk)})
    out.sort(key=lambda c: (0 if c["id"] in _HEAD else 1,
                            _HEAD.index(c["id"]) if c["id"] in _HEAD else 0,
                            c["id"]))
    return out


def _channel_ids():
    return {c["id"] for c in channels()}


# ─── 账本（只记元数据） ──────────────────────────────────

def _cleared_at():
    try:
        with open(_CLEARED_AT, encoding="utf-8") as f:
            return float((f.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0.0


def _ledger_append(row):
    """记一行。落盘失败只警告不抛——账本是附注，不能把用户的图搭进去。"""
    try:
        with _ledger_lock:
            d = os.path.dirname(_LEDGER)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(_LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        log.warning("跑图台账本写不进去 %s", _LEDGER, exc_info=True)


def _archive_back(job, fields):
    """把这张图的结果盖回工作台的聊天记录（2026-11-26）。

    为什么在服务端做：以前只有前端「轮询拿到图 → PATCH 回档」一个写入方，
    用户点了发送就把页面切走 / 大退，那一发永远不会有——图在 output 里躺着、
    记录还停在「正在跑」，重进就是一张假卡（用户原话：「服务端其实已经跑完
    了，但前端没有把这个结果传回来」）。QQ 那边也有过同款「跑完了但没找到
    图片」，根子在同一个地方：**写记录挂在「客户端还在」这个前提上**。

    只影响工作台的档位：`owner` 是 qq 或 `guest_<id>`，跑图台/QQ 的任务
    owner 与任何一档聊天记录都对不上，`write_back` 找不到就直接返回。

    失败只警告：档位写不进去不该让这张图作废（图本身已经在盘上了）。
    """
    try:
        from app.features.chat import routes as chat_routes
        chat_routes.write_back(job.get("owner"), job.get("rec"),
                              job.get("id"), fields)
    except Exception:                                   # noqa: BLE001
        log.warning("兜底写档失败 %s", job.get("id"), exc_info=True)


def _ledger_rows(limit=100):
    """账本行（新的在前），跳过「清空截止」之前的。"""
    cutoff = _cleared_at()
    rows = []
    try:
        with open(_LEDGER, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue          # 坏行跳过，别让一行坏了整本账
                if isinstance(row, dict) and row.get("ts", 0) > cutoff:
                    rows.append(row)
    except OSError:
        return []
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return rows[:max(1, min(int(limit or 100), 200))]


# ─── 任务 ────────────────────────────────────────────────

def _public(job):
    """任务记录脱敏给前端（内部字段不下发）。"""
    return {k: job.get(k) for k in ("id", "skill", "prompt", "seed",
                                    "state", "error", "file", "ts",
                                    "phase", "pid", "stuck",
                                    # 「前面还有几张」（2026-11-27）：
                                    # 排队阶段说人话用，见 ahead_by_tag()。
                                    "ahead",
                                    "steps", "steps_label", "cfg",
                                    "negative", "width", "height",
                                    "size_label", "secs", "landscape",
                                    "ref_note", "lowres",
                                    # 访客模式的回执：前端直接读这个数显示
                                    # 「今日还剩 N 张」，不必再打一趟 /me。
                                    "visitor", "visitor_left")}


def _enabled_loras(job):
    """这张任务实际启用的 LoRA 槽（只留开的，账本行别堆一堆关着的）。"""
    return [{"slot": s["slot"], "name": s.get("name") or "",
             "strength": s.get("strength")}
            for s in (job.get("loras") or []) if s.get("on")]


def _local_file(ref):
    """任务里的图引用 → 本机真实路径；找不到返回空串。

    两种引用：`nai:<文件名>` 是 NAI 云端落的临时文件（`image_out` 目录），
    其余是 ComfyUI output 里的相对路径（可能带 `web_<owner>/` 子目录）。
    和 `/api/gen/img` 取图用的是同一套判据，别在这儿另立一份。
    """
    ref = str(ref or "")
    if not ref:
        return ""
    if ref.startswith("nai:"):
        name = ref[4:]
        if not _IMG_NAME_RE.fullmatch(name):
            return ""
        path = os.path.join(image_out._out_dir(), name)
    else:
        path = comfy_out.local_output_file(ref) or ""
    return path if path and os.path.isfile(path) else ""


def _qq_deliver(job, file_ref):
    """投递这一步本身（`_sync_to_qq` 起的线程体）。

    单独拎出来是为了能直接测：失败那条路只写一句提示，不打 `err`
    ——图本身是好的，只是没送到。
    """
    qq = str(job.get("qq") or "")
    path = _local_file(file_ref)
    if not path:
        log.warning("同步 QQ：%s 的图找不到文件（%s）",
                    job.get("id"), file_ref)
        _archive_back(job, {"note": "图好了，但没找到文件，没发到 QQ"})
        return
    try:
        qq_api.send_image("private", qq, path)
    except Exception as e:                              # noqa: BLE001
        log.warning("同步 QQ 失败（%s → %s）：%s", job.get("id"), qq, e)
        _archive_back(job, {"note": "图好了，但没发到 QQ：%s"
                                    % (str(e) or e.__class__.__name__)})
        return
    log.info("跑图台 %s 已同步到 QQ %s", job.get("id"), qq)


def _sync_to_qq(job, file_ref):
    """出图后顺手往用户自己的 QQ 发一张（工作台「同步发 QQ」，2026-11-27）。

    用户原话：「如果用户已经登录的话……加一个按钮，默认是勾选绿色的，就是
    同步发 QQ」。工作台和 QQ 私聊本来就是同一条模型链路（见 `api_ask`），
    差的只是最后那段投递——这里补上，走 `qq_api.send_image` 的私聊兜底
    （非好友也能落到临时会话）。

    **必须另起线程**：`send_image` 最长等 60 秒（协议端慢 / 图大），压在
    worker 里会让 `finally` 迟落 `_thread_alive`，`_reap_stuck` 会把这张
    当成卡死。图已经好、前端已经在显示，投递成败不该拖住收尾。
    """
    if not job.get("qq_sync") or not job.get("qq"):
        return
    threading.Thread(target=_qq_deliver, args=(job, file_ref),
                     daemon=True).start()


def _run(job):
    """worker 线程体：按渠道走 ComfyUI 或 NAI 队列，出图后记账。"""
    landscape_ok = False
    try:
        if job["skill"] in image_jobs.NAI_SKILLS:
            queued, reason = image_jobs.enqueue(
                "web", job.get("owner") or "anonymous", job["prompt"],
                skill=job["skill"], prompt=job["prompt"], seed=None,
                landscape=(job["skill"] == "nai_wide"),
                out_dir="web_" + str(job.get("owner") or "anonymous"))
            if queued is None:
                raise RuntimeError(reason or "NAI 队列拒绝了这张图")
            queued.cancel = job["abort"]
            # 让 `cancel_tag` 能按本台的任务号找到它（不然「终止」只停得住
            # 本台这份 Event，NAI 那条队列照跑）。替身对象（测试里的 FakeJob）
            # 不一定收得下这个属性——设不上就不设，队列照跑。
            try:
                queued.tag = job["id"]
            except Exception:                   # noqa: BLE001
                pass
            entry = queued.wait()
            paths = (entry or {}).get("images") or []
            if not paths:
                raise RuntimeError("NAI 已完成但没有返回图片")
            path = paths[0]
            name = os.path.basename(path)
            if not _IMG_NAME_RE.fullmatch(name):
                raise RuntimeError("NAI 返回了非法图片名")
            file_ref = "nai:" + name
            seed = None
            steps_label = ""
            size_label = ""
            secs = None
        elif job.get("source_images"):
            # 图生图（2026-11-26）：跟 QQ 的 `generate_image` 同一条骨架。
            # 源图由 `_build_i2i` 缩放好、挂成占位名，真上传在 worker 里做
            # （那时 ComfyUI 一定在线，see `image_jobs._upload_i2i`）。
            wf, i2i_src = _build_i2i(job)
            steps_label = ""
            size_label = ""
            secs = 0
            seed = job["seed"]
            if job["abort"].is_set():
                raise Cancelled("用户中断了这张图")
            mj, reason = image_jobs.enqueue(
                "web", job.get("owner") or "anonymous", wf,
                skill=job["skill"], prompt=job["prompt"], seed=job["seed"],
                out_dir="web_" + str(job.get("owner") or "anonymous"),
                i2i_src=i2i_src, i2i_name=_I2I_PLACEHOLDER)
            if mj is None:
                raise RuntimeError(reason or "没排上队")
            mj.cancel = job["abort"]
            try:
                mj.tag = job["id"]
            except Exception:               # noqa: BLE001
                pass
            entry = mj.wait()
            paths = image_jobs.output_images(entry, job=mj)
            if not paths:
                raise RuntimeError("图生图完成但没有返回图片")
            name = paths[0]
            file_ref = name
        else:
            wf = generate_image_build(job["skill"], job["prompt"], job["seed"])
            if wf is None:
                raise RuntimeError("渠道 %s 没有文生图工作流" % job["skill"])
            # LoRA 槽位（2026-10-28）：只对 `MOSSLoRAToggle` 渠道有内容，
            # 别的渠道 apply 返回 0（找不到那个节点），无副作用。
            if job.get("loras"):
                lora_slots.apply_slots(wf, job["loras"])
                # build_t2i_workflow 是按模板默认拼的 nai5 触发词；面板显式
                # 关了 8 号槽就把前缀剥掉，别留一段没有 LoRA 支撑的空喊。
                if any(isinstance(s, dict) and s.get("slot") == 8
                       and not s.get("on") for s in job["loras"]):
                    lora_slots.strip_nai5_prefix(wf)
            # 自定义步数（2026-10-29）：只改采样节点的 steps，不改别的字段。
            # 不改就是模板默认——`describe` 拿到的是实际生效值，账本和界面
            # 都显示它，用户才知道这张到底是 30 步还是他刚填的 50 步。
            if job.get("steps"):
                n = gen_steps.apply_steps(wf, job["steps"])
                if not n:
                    log.warning("跑图台 %s：渠道 %s 没有可改的步数节点，按模板跑",
                                job["id"], job["skill"])
            steps_label = gen_steps.describe(wf)
            # 自定义 CFG / 负向（2026-10-31）：同样只改内存里的 dict。
            # 顺序在步数之后无所谓，两者不碰同一个字段。负向是**追加**在
            # 模板负向池后面（见 `gen_params` 模块头），不覆盖。
            if job.get("cfg") is not None:
                if not gen_params.apply_cfg(wf, job["cfg"]):
                    log.warning("跑图台 %s：渠道 %s 没有可改的 cfg 节点，按模板跑",
                                job["id"], job["skill"])
            if job.get("negative"):
                if not gen_params.apply_negative(wf, job["negative"]):
                    log.warning("跑图台 %s：渠道 %s 没有可追加的负向节点，已忽略",
                                job["id"], job["skill"])
            # 自定义尺寸（2026-10-31）：只对 moss 渠道受理（提交时已筛过）。
            if job.get("width") and job.get("height"):
                if not gen_params.apply_size(wf, job["width"], job["height"]):
                    log.warning("跑图台 %s：渠道 %s 没有可改的底图节点，按模板尺寸跑",
                                job["id"], job["skill"])
            # 横屏（2026-11-10）：勾上就把画布设成 1920×1080，放大链照常跑。
            # 和上面的手填尺寸互斥（提交时已让位），顺序无关。
            if job.get("landscape"):
                landscape_ok = image_jobs.set_canvas_size(wf, *LANDSCAPE_SIZE)
                if not landscape_ok:
                    log.warning("跑图台 %s：渠道 %s 找不到画布节点，横屏未生效",
                                job["id"], job["skill"])
            size_label = gen_params.describe_size(wf)
            if landscape_ok:
                # describe_size 只认 Empty* 画布；批量类画布认不出时别显示空串。
                size_label = "%d×%d 横屏" % LANDSCAPE_SIZE
            # 工作台的低分辨率档（2026-11-26）：画布降一档、倍率补回来，成品
            # 尺寸不变，省掉第一段在大画布上的采样。**只对工作台**——QQ 和
            # 本地跑图台 `/gen` 要原图。放在横屏 / 手填尺寸之后：那一档自己
            # 有自己的画布要求，不和它抢。
            if (job.get("lowres") and not landscape_ok
                    and not (job.get("width") and job.get("height"))):
                pl = lowres.apply(wf)
                if pl.get("on"):
                    size_label = "%d×%d → %d×%d" % (
                        pl["canvas"][0], pl["canvas"][1],
                        pl["est"][0], pl["est"][1])
                    job["size_label"] = size_label
                else:
                    log.info("跑图台 %s：渠道 %s 不降档（%s）",
                             job["id"], job["skill"], pl.get("why"))
            # _submit_retry 只对超时重试；其余错误一次抛清。
            # with_entry=True：拿 history entry 算 ComfyUI 真跑这张图的耗时。
            #
            # 2026-11-23：`_submit` 多收几个关键字（都可选，不传时它自己造）：
            #   · task_tag=本台任务号 —— 让 `image_jobs.cancel_tag` / status
            #     的 phase 判断能对上「这是哪一张」；
            #   · on_pid=回调 —— ComfyUI 一接单就把 prompt_id 回填到本台 job，
            #     `/api/gen/status` 才能去 ComfyUI 那边问「它在不在队列里」。
            #     这就是用户要的「探活」：不确定就明说掉队了，别一直写画图中。
            #   · batch="workspace"（2026-11-27）——`_submit` 默认写死 "comic"，
            #     不传的话本台每张图都被标成漫画批次，漫画台一点「中断全部」
            #     （cancel_batch("comic")）就把工作台排队的图一起摘掉。这里是
            #     借漫画台的 _submit 拿探活能力，批次归属必须是本台自己的。
            def _on_pid(pid):
                with _JOBS_LOCK:
                    job["pid"] = pid

            name, entry = comic._submit_retry(
                lambda: comic._submit(wf, job["skill"], job["prompt"], job["seed"],
                                      with_entry=True, abort=job["abort"],
                                      task_tag=job["id"], on_pid=_on_pid,
                                      batch="workspace"),
                "[跑图台 %s]" % job["id"])
            # 采完样才出的图，也可能已经被终止（_submit 在回调里看 abort 会抛）
            if job["abort"].is_set():
                raise Cancelled("用户中断了这张图")
            secs = image_jobs.exec_seconds(entry)
            file_ref = name
            seed = job["seed"]
        row = {"tag": job["id"], "skill": job["skill"],
               "prompt": job["prompt"], "seed": seed,
               "file": file_ref, "ts": job["ts"], "ok": True,
               "steps": steps_label, "secs": secs,
               "loras": _enabled_loras(job)}
        # 只记「这次真的改过」的字段（NAI 云端没有本地 cfg / 负向，
        # 不写空值，免得历史行里全是 cfg: null 的噪声）。
        if job.get("cfg") is not None:
            row["cfg"] = job["cfg"]
        if job.get("negative"):
            row["negative"] = job["negative"]
        if job.get("width") and job.get("height"):
            row["size"] = size_label or ("%d×%d" % (job["width"], job["height"]))
        if landscape_ok:
            row["landscape"] = True
            row["size"] = size_label
        if job.get("size_label"):
            # 低分辨率档只有一个「画布 → 成品」的写法，账本里得留下这个数，
            # 否则以后想对比「降档到底快多少」时没有依据。
            row["size"] = job["size_label"]
        _ledger_append(row)
        with _JOBS_LOCK:
            job["state"] = "done"
            job["file"] = file_ref
            job["seed"] = seed
            job["steps_label"] = steps_label
            job["size_label"] = size_label
            job["secs"] = secs
        log.info("跑图台 %s 完成：%s seed %s %s 步 cfg %s %s ComfyUI 耗时 %s 秒",
                 job["id"], job["skill"], seed, steps_label or "-",
                 job.get("cfg") if job.get("cfg") is not None else "-",
                 size_label or "-",
                 "%.1f" % secs if secs is not None else "未知")
        # 服务端自己把结果写回这一档：前端在不在都一样（见 `_archive_back`）。
        _archive_back(job, {"file": file_ref, "note": "", "err": False,
                            "job": ""})
        # 工作台「同步发 QQ」（2026-11-27）：图好了再投递，投递不占 worker。
        _sync_to_qq(job, file_ref)
    except Cancelled:
        # 用户点了「中断」：队列里那张已由 wait() 放弃等待、worker 会跳过；
        # 正在画的由 image_jobs 定向 /interrupt 后收工。这里只改状态，不记账、
        # 不写失败——主动停的不算「出图失败」。
        with _JOBS_LOCK:
            job["state"] = "cancelled"
            job["error"] = "已中断"
        log.info("跑图台 %s 已中断", job["id"])
        # 档位里那条也要收尾：不然重进还写「正在跑」，用户以为它还在画。
        _archive_back(job, {"note": "已终止", "err": False, "job": ""})
    except Exception as e:
        msg = str(e) or e.__class__.__name__
        with _JOBS_LOCK:
            job["state"] = "error"
            job["error"] = msg
        _ledger_append({"tag": job["id"], "skill": job["skill"],
                        "prompt": job["prompt"], "seed": job["seed"],
                        "file": "", "ts": job["ts"], "ok": False,
                        "error": msg})
        log.warning("跑图台 %s 失败：%s", job["id"], msg)
        # 真失败（服务端明确说了不行）才打 err，重进时直接显示这句 + 重画。
        _archive_back(job, {"note": msg, "err": True, "job": ""})
    finally:
        # 线程退场了（成功 / 失败 / 被中断都算）。`_reap_stuck` 靠这个标记
        # 认「这张已经没人管了」——不置的话，任何一条异常退出的路径都会
        # 变成前端永远「排队中」的那张图。
        with _JOBS_LOCK:
            job["_thread_alive"] = False


_I2I_PLACEHOLDER = "__I2I_SRC__"   # 与 `generate_image._I2I_SRC_PLACEHOLDER` 同值


def _data_url_bytes(url):
    """`data:image/png;base64,...` → 原始字节。工作台的上传图是这个形态。"""
    s = str(url or "")
    if not s.startswith("data:"):
        raise RuntimeError("源图格式不认识")
    head, _, body = s.partition(",")
    if ";base64" not in head:
        raise RuntimeError("源图不是 base64")
    raw = base64.b64decode(body)
    if not raw:
        raise RuntimeError("源图是空的")
    return raw


def _build_i2i(job):
    """工作台的图生图：按渠道取 i2i 骨架，把源图缩好挂成占位名。

    返回 `(workflow, 源图字节)`。规则**照抄** `generate_image` 里那段 i2i
    分支，不另立一套——两处骨架要是各写一份，早晚一个改了一个没改：

      - 骨架：qwen 用 `workflow_i2i_qq.json`（1248 面积档 / 真人 LoRA），
        其余渠道用 `workflow_i2i.json`
      - 重绘强度：qwen 1.0（零 latent 架构，0.85 会出平铺废图），其余 0.6
      - 缩放：按该渠道文生图画布的长边归一化（EXIF 方向 / 透明通道 / 8 的倍数）
      - 上传：这里**只填占位名**，真上传在 worker 里做——那时 ComfyUI 一定
        在线（见 `image_jobs._upload_i2i`）

    渠道没有 i2i 骨架 → 直接报错。QQ 那边是静默降级成文生图（用户看不见
    后台，白等一轮也得给张图）；工作台相反——他传了图就是想改这张，给他一张
    凭空画的才是骗人。
    """
    import json
    from app.tools.normal import generate_image as gi
    from app.features.image import comfy_src

    skill = job["skill"]
    if skill not in gi._I2I_SKILLS:
        raise RuntimeError("「%s」不支持图生图，换一个画风再试" % skill)
    sd = gi.load_skill(skill)
    if not sd:
        raise RuntimeError("画风 %s 没加载起来" % skill)
    name = ("workflow_i2i_qq.json" if skill == "qwen_image_v1"
            else "workflow_i2i.json")
    i2i = gi.load_workflow(os.path.join(sd["path"], name))
    if not i2i:
        raise RuntimeError("画风 %s 没有图生图工作流" % skill)
    fitted, _size = comfy_src.fit(
        _data_url_bytes(job["source_images"][0]),
        max_side=gi._canvas_long_side(sd["workflow"]) or comfy_src.MAX_SIDE)
    # 先填骨架自带的两个占位（源图名 / 重绘强度），再填提示词与种子。
    # 顺序无所谓——两组占位不重叠；但源图名必须是**占位串**，真名在 worker
    # 上传完才回填（`_upload_i2i` 按 JSON 字符串整段替换）。
    wf = json.loads(json.dumps(i2i).replace(
        json.dumps("__SOURCE_IMAGE__"), json.dumps(_I2I_PLACEHOLDER)).replace(
        json.dumps("__DENOISE__"), json.dumps("%.2f" % gi._i2i_denoise(skill))))
    wf = json.loads(gi.apply_prompt_placeholders(
        json.dumps(wf), job["prompt"], job["seed"] or 0))
    if job.get("loras"):
        lora_slots.apply_slots(wf, job["loras"])
    return wf, fitted


def generate_image_build(skill, prompt, seed):
    """包一层只为测试好 patch；逻辑就是 build_t2i_workflow。"""
    from app.tools.normal import generate_image
    return generate_image.build_t2i_workflow(skill, prompt, seed)


# ─── 路由 ────────────────────────────────────────────────

@bp.route("/gen")
def page():
    """跑图台页面（静态文件 `web/gen_pad.html`，跟漫画工作台并列）。

    2026-11-23 一度 302 到新界面 `/workspace/`，现已改回原始跑图台：
    5174 本机是站主自用、要的就是这套旧页面的详细参数面板；新界面只在
    5175 群友页发。`/workspace/` 仍可直达，只是不再是 `/gen` 的去处。
    """
    return send_from_directory(WEB_DIR, "gen_pad.html")


@bp.route("/api/gen/channels")
def api_channels():
    return jsonify({"ok": True, "channels": channels()})


@bp.route("/api/gen/loras")
def api_loras():
    """这条渠道的 LoRA 槽位。

    目前只有 moss（`MOSSLoRAToggle` 多槽开关）有内容；别的渠道返回空列表，
    前端据此决定显不显示面板——和 `comic_render_opts.caps()['lora_kind']`
    同一套判定，别在这里另立标准。
    """
    skill = str(request.args.get("skill") or "").strip()
    if skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r" % skill}), 400
    wf = (load_skill(skill) or {}).get("workflow") or {}
    if not lora_slots.has_toggle(wf):
        return jsonify({"ok": True, "skill": skill, "kind": "", "slots": []})
    return jsonify({"ok": True, "skill": skill, "kind": "toggle",
                    "slots": lora_slots.read_slots(wf)})


@bp.route("/api/lora/save", methods=["POST"])
def api_lora_save():
    """把一套 LoRA 槽位写回渠道模板（之后所有台 + QQ 侧都按它跑）。

    三个台的「存为默认」都走这一个口：跑图台、漫画台、MOSS 台只是提交入口不同，
    落点必须同一个，否则「在 A 台存了默认、B 台不认」只是时间问题。
    """
    data = request.get_json(silent=True) or {}
    skill = str(data.get("skill") or "").strip()
    if not skill or skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r" % skill}), 400
    slots, errs, warns = lora_slots.normalize(data.get("slots") or data.get("loras"))
    if errs:
        return jsonify({"ok": False, "error": "；".join(errs)}), 400
    if not slots:
        return jsonify({"ok": False, "error": "没有要保存的槽位"}), 400
    with _LORA_SAVE_LOCK:
        ok, msg = lora_slots.save_default(skill, slots)
    return jsonify({"ok": ok, "message": msg, "warnings": warns}), \
        (200 if ok else 500)


@bp.route("/api/gen/ask", methods=["POST"])
def api_ask():
    """工作台的**一句话**：交给大大怪判意图，要画就直接排队（2026-11-26）。

    为什么要有它（用户原话：「工作台其实就是 QQ 这一边……本质上工作台它就是
    跟大大怪一样的呀，相当于大大怪弄到网页端而已」）：
    `/api/gen/submit` 只做「中文→英文」翻译，然后直通 ComfyUI，模型那一层
    整个没参与。于是网页端拿不到 QQ 有的那些能力——改词（「换成白色毛衣」要
    在【上一张】的词上替换服装段）、指代、该不该画、要不要回话。这条接口把
    请求交给 `direct_gen.decide`，用的就是 QQ 私聊那一套（同一条
    `_MASTER_TEMPLATE`、同一份 6 轮历史窗口），只是把结果交回网页而不是 QQ。

    **不是另起一条链**：翻译/改写/渠道判定/入队全在 `decide` 里，这边只做
    「验身份 → 绑会话上下文 → 记历史 → 依次试两项新能力 → 不行回落到
    `submit` 的老行为」。回落到 submit 是刻意的：跑图台的 LoRA 槽位、步数、
    CFG、横屏、画师串这些具体参数，`decide` 那条链路不认，只有 submit 有。

    返回三种：
      - `{"ok": true, "job": {...}}`            要画，已排队（前端按 id 轮询 status）
      - `{"ok": true, "reply": "…", "say": true}` 模型判这轮不画，要回一句（前端显示气泡）
      - `{"ok": false, "error": "…", "busy": bool}` 没排上 / 转译失败
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "说一句想画什么"}), 400
    if len(text) > _MAX_PROMPT:
        return jsonify({"ok": False,
                        "error": "太长了（%d 字，上限 %d）"
                                 % (len(text), _MAX_PROMPT)}), 400
    skill = str(data.get("skill") or "").strip()
    if skill and skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r" % skill}), 400
    from app.features.chat import routes as chat_routes
    qq = chat_routes._who() or ""
    # 访客模式（2026-11-26）：没有 QQ，也没登录过。额度按 5175 签的访客
    # 身份算，每天 5 张，**在提交这一刻扣**（干完再扣就成了「排队中不占额度」
    # 可以被刷）。额度不够就一句人话，别让它跑完再报错。
    #
    # ⚠️ 这里原先写成 `if not qq: 401 没登录`，把访客当成了没登录：访客在
    # 聊天页发「画一个可莉西装」，蓝色气泡下面直接挂一行「没登录 · 重画」，
    # 图发不出去。访客**有身份**，只是身份不在 qq 上——先认访客再判空。
    gid = _visitor.current()["id"]
    if not qq and not gid:
        return jsonify({"ok": False, "error": "没登录"}), 401
    if gid:
        left = _visitor.remaining(gid)
        if left <= 0:
            return jsonify({"ok": False, "visitor": True,
                            "error": "访客每天 5 张，今天已经用完了"}), 403
    # 模型那套接口要一个「这轮是谁」的键：访客用身份段拼一个，别给空串——
    # 空串会让所有访客共用一条会话（历史、上一张图、指代全串）。
    who = qq or _guest_key(gid)
    refs = data.get("refs")
    # 访客走**原尺寸**：用户 2026-11-26 明确「访客不用走降档，没有意义，
    # 浪费时间」——他们把全部渠道都开着给访客玩，要的就是成品本身。
    ctx = {"images": _web_images(refs, qq), "refs": list(refs or []),
           # 「引用那张图的提示词」（2026-11-27）：卡片上的引用不是传图，
           # 是把这张图当初那串提示词交给模型改写。有它就走文生图。
           "anchor": str(data.get("anchor") or "").strip()[:1000],
           "lowres": (not gid) and WORKSPACE_LOWRES,
           # 前端那条聊天记录的 id（2026-11-26）：任务结束那一刻服务端要靠它
           # 把结果盖回档位，页面关掉也不会丢图。
           "rec": str(data.get("rec") or "").strip(),
           # 「同步发 QQ」（2026-11-27）：开关在图卡右下角，只有登录用户看得见。
           # 访客 qq 是空串，`_sync_to_qq` 会直接跳过——这里不另判一遍。
           "qq_sync": bool(data.get("qq_sync")),
           "qq": qq}
    hist = _web_history(who)
    # ⚠️ `bind_context` 是**赋值 + 返回 None**，不是上下文管理器（它把当前
    # 会话写进线程本地，靠 finally 里的 `clear_context` 收尾）。按 QQ 那边
    # 的老用法一模一样地写：绑 → 干活 → finally 清。
    qq_api.bind_context("private_%s" % who, "private", who,
                        user_text=text,
                        own_images=ctx["images"] or None,
                        quoted_images=ctx["images"] or None)
    try:
        job = _web_decide(text, skill, hist, ctx)
    except _WebReply as e:
        # 模型说这轮不画（纯聊天 / 问问题）：**不扣额度**，只有真出图才扣。
        _web_history_push(who, text, str(e.reply))
        return jsonify({"ok": True, "reply": str(e.reply), "say": True})
    except RuntimeError as e:
        # 没名额 / 模型没交作业 → 一句话说清。**不回落直通**：已经花了一次
        # 转译，再来一遍只会把同一件事翻第二次，还翻得更差。
        return jsonify({"ok": False, "busy": True, "error": str(e) or
                        "没排上队，等一下再发"}), 429
    finally:
        try:
            qq_api.clear_context()
        except Exception:
            pass
    _web_history_push(who, text, "", job.get("prompt") or text,
                      job.get("skill") or "")
    job["visitor"] = bool(gid)
    if gid:
        ok, left = _visitor.consume(gid)
        if not ok:
            # 极窄的竞态（同一秒内开了两个标签页）：图已经排上了，不撤——
            # 撤掉反而是「扣了额度没图」更糟。这里只把剩余数照实回给前端。
            left = _visitor.remaining(gid)
        job["visitor_left"] = left
    return jsonify({"ok": True, "job": _public(job)})


# ─── 直达：同一串词原样进工作流（2026-11-26）─────────────────────────
#
# 粉丝群反馈「同一个提示词连续跑 5 张，5 张图都不一样」——因为点发送会先让
# 大大怪优化提示词。所以给一个**不经过模型**的入口：粘进来的词原样进模型。
# 2026-11-27 用户拍板：直达一次就一张；早先的「连排 5 张」不是直达的本意。
#
# `/api/gen/batch` 仍保留「一次建 N 条」的接口能力，缺省 1 条；和 submit
# 共用同一个 `_enqueue_job`（限额、记账、终止、轮询全是一份实现）。前端
# 直达按钮传 count=1，想连多张可以显式传 count，但按钮默认不连排。
#
# seed：`_enqueue_job` 本来每条就随机一个，天然不同，所以这里**不指定**
# 同一个 seed，也不让前端传——「连跑 N 张相同」正是要解决的问题。
@bp.route("/api/gen/batch", methods=["POST"])
def api_batch():
    """原样提示词排 N 张（直达按钮传 1）。→ {ok, jobs:[...], fail:[…], remain?}

    部分失败照实回：排队名额不够时先建能建的，剩下的写进 `fail`，前端按
    「已发 3 张，还有 2 张没排上：<原因>」提示。**不整批回滚**——已经排上的
    那几张撤下来只会让用户更糊涂。
    """
    data = request.get_json(silent=True) or {}
    skill = str(data.get("skill") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    if skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r（刷新页面重载渠道列）"
                        % skill}), 400
    if not prompt:
        return jsonify({"ok": False, "error": "提示词是空的"}), 400
    try:
        want = int(data.get("count") or 1)
    except (TypeError, ValueError):
        want = 1
    want = max(1, min(want, image_jobs.MAX_INFLIGHT, 10))
    # 中文原样进 CLIP = 等于没给提示词。直达的卖点是「原样」，但「原样」
    # 指的是**不被模型改写**，不是不看渠道语种——跟 submit 同一套判断：
    # 渠道要英文、而用户写的是中文时仍然翻一遍，否则出的是噪声图。
    final = bool(data.get("prompt_is_final"))
    body_prompt = prompt
    if not final and _needs_translate(skill, body_prompt):
        en = _translate_prompt(skill, body_prompt)
        if en:
            body_prompt = en
    if len(body_prompt) > _MAX_PROMPT:
        return jsonify({"ok": False,
                        "error": "提示词太长了（%d 字，上限 %d）"
                                 % (len(body_prompt), _MAX_PROMPT)}), 400

    from app.features.chat import routes as chat_routes
    qq = chat_routes._who() or ""
    # 访客（2026-11-26）：跟 `api_ask` 同一套身份口径。原先这里没有「没登录
    # 就不给排」的判断，访客能直接打这条接口把额度用掉——现在补上，
    # 并让访客的会话线/账本键落在身份段上（`__anonymous__` 会让所有访客共用
    # 一条历史，直达那轮记的上下文跑到别人身上）。
    gid = _visitor.current()["id"]
    if not qq and not gid:
        return jsonify({"ok": False, "error": "没登录"}), 401
    owner = qq or _guest_key(gid)

    # 访客：额度按**真的要建几张**扣。不够就只建剩下的那几张（额度 5 张、
    # 今天还剩 2 张、但要 5 张 → 建 2 张），并明说一句，别让他以为全排上了。
    fits = want
    if gid:
        left = _visitor.remaining(gid)
        if left <= 0:
            return jsonify({"ok": False, "visitor": True,
                            "error": "访客每天 %d 张，今天已经用完了"
                                     % _visitor.IMG_PER_DAY}), 403
        fits = min(want, left)

    jobs, fail = [], []
    for _ in range(fits):
        job, busy = _enqueue_job(
            {"skill": skill, "prompt": body_prompt,
             "ref_note": "",
             # 直达连排 N 张：用户勾着开关就每张都发（2026-11-27 用户拍板
             # 「只要他有这个开关，直达也好其他也好，都正确发图」）。
             "qq_sync": bool(data.get("qq_sync")) and bool(qq),
             "qq": qq if data.get("qq_sync") else ""},
            owner=owner,
            # 访客走原尺寸（用户 2026-11-26：「访客不用走降档」）
            lowres_on=(not gid) and WORKSPACE_LOWRES)
        if job is None:
            fail.append(busy or "没排上队")
            break
        jobs.append(job)
    if not jobs:
        return jsonify({"ok": False, "busy": True,
                        "error": fail[0] if fail else "没排上队"}), 429
    for _ in jobs:
        if gid:
            _visitor.consume(gid)
    # 历史只记一轮：这一句用户只说了**一次**，记成 N 轮会让下一轮模型以为
    # 他连着问了好几遍（改词的上下文就乱了）。
    _web_history_push(owner, prompt, "", jobs[0].get("prompt") or body_prompt,
                      skill)
    out = [_public(j) for j in jobs]
    for j in out:
        j["visitor"] = bool(gid)
    resp = {"ok": True, "jobs": out, "fail": fail,
            "count": len(jobs), "asked": want}
    if want > fits:
        resp["note"] = "访客今天只剩 %d 张，这次先排了 %d 张" % (fits, fits)
    # 排队名额撞满（一次最多 MAX_INFLIGHT）：建了几张就是几张，剩下的一句
    # 人话说明，别让用户以为已经排满了。
    if want > len(jobs) and not resp["fail"]:
        resp["fail"].append("一次最多排 %d 张" % image_jobs.MAX_INFLIGHT)
    if gid:
        left = _visitor.remaining(gid)
        for j in out:
            j["visitor_left"] = left
        resp["remain"] = left
    return jsonify(resp)


# ─── 工作台「一句话」通道（见 api_ask）────────────────────────────────
# 和 QQ 的关系：**同一套转译、同一份历史规则、不同的落盘位置**。
#   - 转译：`direct_gen._translate`，一个字都不改（用户 2026-11-26：
#     「我要求就是提示词那一套东西，要跟 QQ 一样」）。
#   - 历史：QQ 那份是 `agent_store` 下 `session_key = private_<号>` 的会话文件，
#     里面混着群聊、别处私聊的记录。工作台单独一份（`session_key =
#     web_<号>`），同样裁 6 轮、同样不带 AI 自己的回执行——各存各的，
#     互不串味（用户当次拍板）。
#   - 上一张：`image_log.latest_for("private", qq)`，与 QQ 私聊**同一个键**。
#     改了词接着改，跨端也是同一张图的延续。

_WEB_ASK_TURNS = 6          # 喂给模型的轮数（与 QQ_BOT_TURNS 同值）

# 访客在模型那套接口里挂的名字（2026-11-26）。访客没有 QQ，但 `_translate`
# / `_revise` / `bind_context` 都要一个「这轮是谁」的键：给空串的话所有访客
# 会挤进同一条会话、互相看见对方的提示词，指代（「换成白色毛衣」）也会串到
# 别人的上一张上去。用 5175 签的身份段拼一个出来，同一个访客前后一致。
def _guest_key(gid=""):
    gid = gid or _visitor.current()["id"]
    return ("guest_" + gid) if gid else ""


_WEB_IMG_CACHE = {}         # chat_ref_id -> data URL（临时参考图，见 _web_ctx）
_WEB_IMG_LOCK = threading.Lock()
_WEB_IMG_MAX = 12           # 一个用户最多藏 12 张，超了丢最老的
_WEB_IMG_TTL = 2 * 3600     # 两小时没人用就丢（工作台一轮一般几分钟）


def _gallery_ref_data(ref_id, qq):
    """画廊私有参考图 → data URL；不是自己的 / 过期 / 读失败返回 ""。

    `ref_id` 是 `/api/gal/ref` 上传后返回的那个（工作台「＋」相册与拍照都走
    它）。归属判据全交给 `gallery._read_private_image()`，这里不另写一套。
    """
    if not qq:
        return ""
    try:
        from app.features.gallery import routes as gal
        from app.integrations import vision as vision_mod
        path = gal._read_private_image(ref_id, qq)
        if not path:
            return ""
        with open(path, "rb") as f:
            return vision_mod.to_data_url(f.read())
    except Exception:                                  # noqa: BLE001
        log.warning("工作台读参考图失败（ref_id=%s）", ref_id, exc_info=True)
        return ""


def _gallery_item_data(iid, qq):
    """画廊作品图（item id，数字）→ data URL；没权限 / 读失败返回 ""。

    「画同款」交上来的就是这个 id——它不是上传进来的参考图，而是画廊里
    已经出好的那一张。权限判据全在 `gallery._read_image_data()` 里。

    `full=True`（2026-11-27 用户最终拍板）：**引用就是引用，这里不判身份**。
    可见性只在画廊列表判一次——访客看不到隐藏 / 下架，登录用户看到全部；
    凡是页面上点得开、点得到「做同款」的图，就有权拿它反推生成。生图入口
    再判一次访客 / 登录没有意义：列表里看得到、点得着，提交时却被挡回去，
    出来的图只会跟原图无关（用户 21:59 / 22:01 两次报的就是这个）。
    `/img`、`/describe` 这些**展示面**不传 `full`，仍按 `_full(qq)` 判。
    """
    try:
        from app.features.gallery import routes as gal
        return gal._read_image_data(iid, qq, full=True)
    except Exception:                                  # noqa: BLE001
        log.warning("工作台读作品图失败（id=%s）", iid, exc_info=True)
        return ""


def _web_images(refs, qq=""):
    """本轮参考图 → 真正交给模型看的图（data URL）。

    三种 id 都认（2026-11-27）：
      · `cr_<32 位>`：`/api/gen/chat-upload` 存的聊天临时图，在进程内缓存里；
      · 画廊私有 `ref_id`（字符串）：`/api/gal/ref` 上传返回的，读盘转 data URL；
      · 画廊作品 **item id**（数字）：「画同款」传的就是它。

    以前只认第一种，而工作台上传走的是第二种、画同款走的是第三种 →
    `images` 恒为空，后端判「没图」直接纯文生图，用户传的图**一次都没进过
    模型**（2026-11-27 查证：日志里 `vision` 只有前端那次预读，`/api/gen/ask`
    之后没有任何识图）。
    """
    from app.features.gallery import references
    urls = []
    for r in (refs or [])[:3]:                    # 视觉接口一次最多 3 张
        if r is None or isinstance(r, bool):
            continue
        if isinstance(r, str):
            if not r:
                continue
            if references.valid_chat_ref(r):
                with _WEB_IMG_LOCK:
                    row = _WEB_IMG_CACHE.get(r)
                    if row:
                        row["at"] = time.time()
                        urls.append(row["data"])
                continue
            if r.isdigit():                       # 数字 id 被写成字符串也算
                r = int(r)
            else:
                data = _gallery_ref_data(r, qq)
                if data:
                    urls.append(data)
                continue
        if isinstance(r, int):
            data = _gallery_item_data(r, qq)
            if data:
                urls.append(data)
    return urls


def _web_decide(text, skill, hist, ctx):
    """工作台这一轮 → 一张已入队的图，或一句「这轮不用画」。

    **三条路**（2026-11-27 用户拍板，按「图有没有真的交给模型」重划）：

        · **上传了图 / 画廊「画同款」** → `direct_gen._tags_with_request`：
          图和用户那句话一起进**多模态模型**，**一次调用**边看边写词，直接
          入队。用户原话：「一个识图模型，它本身就能够拿到图片和用户的需求，
          然后去跑图……两次调用反而是最亏的」。
        · **没图** → `direct_gen._translate`（QQ 那条主模板）
        · **引用了自己出的图**（`ctx["anchor"]` 有值）→ 也走 `_translate`：
          引用的是**那张图当初的提示词**，不是图的字节。把原词当「上一张」
          交给主模板，模型按用户新说的一句改写它、纯文生图重跑。用户原话
          （2026-11-27）：「引用的其实是那一个提示词……AI 就直接去改提示词，
          然后重新跑一遍就可以了」。这条路不垫图，所以也不会撞上「渠道没有
          图生图骨架」那条报错。

    带图那条**不再走 `_web_revise`**（QQ 的改图管道：先识图、再在原词上改、
    还要模型判要不要垫图）。它两次调用，而且第二步拿不到图；工作台要的是
    「图 + 需求一次成词」。函数本身保留，QQ 那条链还在用。

    **渠道由界面定死**：画风抽屉选了哪个，就用哪个出图——`_translate` 的
    `skill` 入参本来就是「代码点名的 > 模型判的」；这里再把模型自己填的那个
    渠道覆盖掉，模型只负责写提示词。

    没有第三个分支（以前那套「带 LoRA / 步数 / CFG / 横屏就回落旧路」是
    错加的：这些控件根本不在这个页面上，`web/workspace/` 里一个都不传）。
    """
    from app.features.image import direct_gen as dg

    # 模型跟着**管理页给 QQ agent 选的那个**走（2026-11-27）。此前这里裸调
    # `_translate`，provider 全默认 → 落回 `.env` 的全局降级链，第 1 个候选
    # 是已经挂掉的中转站（hfapi.cloud），每次都要**白等 30 秒读超时**才轮到
    # 第 2 个 deepseek 官方（实测官方 0.3 秒就回）。用户 10:37:45 那一发就是
    # 这样：点发送 → 13 秒了还在「正在提交」。QQ 那边 `decide(agent_id=…)`
    # 一直是这么传的，所以 QQ 快、工作台慢。
    provider, model, strict = dg._llm_target(QQ_AGENT_ID)
    use = skill or ""                      # 界面选的渠道；空 = 没选（用默认）
    # 引用提示词（2026-11-27 用户拍板，原话：「引用的其实是那一个提示词……
    # AI 就直接去改提示词，然后重新跑一遍就可以了」）：引用**不是**图生图。
    # 那张图当初的提示词交给主模板当【上一张】，模型在新需求上改写它，
    # 然后纯文生图重跑——所以这条路根本不该带图、也不该进 `_revise`。
    anchor = str(ctx.get("anchor") or "").strip()
    # 带了参考图却一张都没读出来 → **说出来**，不许静默按纯文字画
    # （2026-11-27 用户拍板）。以前这里直接落到下面的 `_translate`，模型拿着
    # 用户那句「照着这张画」加几轮历史自由发挥，出来的图跟原图毫不相干的
    # 同时还带上了历史里的画面内容 —— 用户只会觉得「识图坏了」，却看不到
    # 任何出错提示。宁可让他重点一次，也别给一张错图。
    if not anchor and (ctx.get("refs") or []) and not ctx.get("images"):
        raise RuntimeError("这张图没读到，重发一次")
    if ctx.get("images") and not anchor:
        # 上传图 / 画廊「画同款」：**一次**多模态调用（2026-11-27）。
        # 图跟着 prompt 一起进识图模型，模型边看边按用户要求写词，直接入队。
        # 不再让前端先识一遍、后端再转一遍——那两次里第二次根本没看到图。
        tags = dg._tags_with_request(ctx["images"], text)
        if not tags:
            raise RuntimeError("这张图没读出来，换个说法再发一次")
        got = {"skill": use or "", "prompt": tags}
    else:
        got = dg._translate(text, hist, skill=use or None,
                            provider=provider, model=model, strict=strict,
                            anchor=anchor or None)
    if not got:
        # 模型没交作业（空返回 / JSON 没解析出来）。这是可用性问题，不是它
        # 判「不画」——判不画会走下面的 reply。重来一次比出一张错图强。
        raise RuntimeError("这次没接住，把需求再发一遍试试")
    reply = str(got.get("reply") or "").strip()
    if reply and not got.get("prompt"):
        raise _WebReply(reply)
    # 界面定了渠道就是它：模型填的（`_revise` 的 skill / `_translate` 的
    # 兜底渠道）一律不算数。没选才用模型判的那个。
    out = use or str(got.get("skill") or "").strip()
    if not out:
        raise RuntimeError("没认出用哪条渠道出图，先在「画风」里选一个")
    prompt = str(got.get("prompt") or "").strip()
    if not prompt:
        raise RuntimeError("这次没接住，把需求再发一遍试试")
    body = {"skill": out, "prompt": prompt,
            "user_text": text,
            "refs": [r for r in (ctx.get("refs") or [])
                     if isinstance(r, str) and r.startswith("cr_")]}
    if ctx.get("rec"):
        body["rec"] = ctx["rec"]
    # 同步发 QQ（2026-11-27）：开关一路带到 job 上，出图后由 `_sync_to_qq`
    # 投递。只走 ask 这条主链——「直达」是连排 N 张，全发一遍等于刷屏。
    if ctx.get("qq_sync"):
        body["qq_sync"] = True
        body["qq"] = ctx.get("qq") or ""
    if got.get("source_image"):
        # 模型判「要垫这张图」（图生图 / 照着画 / 复刻）。前端把图以 data
        # URL 交回来，`_enqueue_job` → `_run` 会带着它进 `generate_image`。
        body["source_images"] = ctx["images"]
    job, busy = _enqueue_job(body, owner=_owner_key(),
                             lowres_on=bool(ctx.get("lowres", WORKSPACE_LOWRES)))
    if job is None:
        raise RuntimeError(busy or "没排上队，等一下再发")
    return job


def _web_revise(text, channel, hist, images, provider=None, model=None,
                strict=False):
    """带图的轮次：走 QQ 的 `_revise`，再按界面的渠道把结果钉死。

    `_revise` 内部自己会 `_enqueue`（它原本是给 QQ 用的、入队也在里面），
    所以这里不能直接调它——那样会绕过工作台自己的任务表与轮询接口。
    做法：先把它那条「入队」换成「只返回参数」，拿到 skill / prompt /
    source_image，再交给 `_web_decide` 统一入队。
    """
    from app.features.image import direct_gen as dg

    captured = {}

    def _capture(skill, prompt, user_text="", seed=None, source_image=None):
        captured.update({"skill": skill, "prompt": prompt,
                         "source_image": source_image})
        return ""

    real = dg._enqueue
    dg._enqueue = _capture
    try:
        out = dg._revise(text, images, hist, channel=channel or None,
                         provider=provider, model=model, strict=strict)
    finally:
        dg._enqueue = real
    if captured:
        return captured
    # `_revise` 判定「这轮不是改图请求」（只评价这张图 / 问别的）→ 它返回
    # 的是一段给用户看的话，原样当回话播出去。
    out = str(out or "").strip()
    return {"reply": out} if out else None


def _who_qq():
    from app.features.chat import routes as chat_routes
    return chat_routes._who() or ""


def _owner_key():
    """入队 / 认领 / 状态查询**同一条身份口径**（2026-11-26）。

    `_who_qq()` 对访客是空串，而 `/api/gen/status` 对访客按 `guest_<id>`
    收口、adopt 也按 `_owner()` 找人。ask 这条路原先用 `_who_qq()`，于是访客
    在聊天页发的那张：任务在跑、owner 却是空串 → 状态里查不到 → 前端 15 秒
    后判「服务重启过，这张没了」，等真出图时记录已经被写成失败，认不回来
    （实测 21:58:40 图正常落盘、记录却是 err）。跟 `api_submit` 用同一个口子。
    """
    from app.features.chat import routes as chat_routes
    return chat_routes._owner() or "__anonymous__"


class _WebReply(Exception):
    """模型说这轮不画；带着要回的那句话往外抛（见 `_web_decide`）。"""

    def __init__(self, reply):
        Exception.__init__(self, reply)
        self.reply = reply


def _web_history(qq):
    """工作台自己的会话线（6 轮，与 QQ 同规则）。

    落盘在 `agent_store` 的 `web_<号>` 会话文件里，**不带 system 头**——
    转译用的模板是 `_MASTER_TEMPLATE`，跟 QQ 人设文件无关，塞 head 只会污染
    `_recent_lines` 之外的地方。这里只存 `user` / `assistant` 两种消息，
    读的时候过一遍 `strip_noise` + 按 6 轮硬裁。

    访客没有 QQ（`_who()` 回空串）：不能拿空串拼文件名——`web_chat_.jsonl`
    一个文件会被所有访客共写共读，等于让陌生人互相看见对方的提示词。所以
    访客按 5175 签的身份段分线（`web_guest_<id>.jsonl`），跟额度账本同一把
    身份钥匙。
    """
    from app.features.chat import memory
    path = _web_history_path(qq)
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    out = memory.strip_noise(out)
    return memory.trim_window(out, hard_turns=_WEB_ASK_TURNS)


def _web_history_path(qq):
    # `state_path` 只收一个文件名（不建子目录）：工作台的会话线直接铺在
    # state/ 下，名字带前缀，跟 `image_log.jsonl` 那批同层。
    if not qq:
        # 访客：按身份段分线（见 `_web_history` 的说明）。身份坏了就退到
        # 一个公共的匿名线——宁可上下文串一点，也不能因为文件名非法写崩。
        key = _guest_key() or "anon"
        return state_path("web_chat_%s.jsonl" % key)
    return state_path("web_chat_%s.jsonl" % qq)


def _web_history_push(qq, user_text, reply, prompt="", skill=""):
    """落一轮。**只落用户原话**（+ 模型真出图时的那串提示词当事实）。

    为什么不把 AI 的回话写进 assistant：工作台这一轮的 reply（「这句不用
    画，因为…」）一旦回喂，下一轮模型会顺着继续拒——跟 QQ 那边 2026-11-26
    排查出来的是同一个坑。所以历史里 assistant 只有一条「画了什么」，没有
    「说了什么」。
    """
    path = _web_history_path(qq)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    rows = _web_history(qq)
    rows.append({"role": "user", "content": user_text})
    if prompt:
        rows.append({"role": "assistant",
                     "content": "[工作台出图] %s：%s"
                                % (skill or "默认", str(prompt)[:400])})
    from app.features.chat import memory
    rows = memory.trim_window(rows, hard_turns=_WEB_ASK_TURNS)
    with _SAVE_TMP_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for m in rows:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


_SAVE_TMP_LOCK = threading.Lock()


@bp.route("/api/gen/chat-upload", methods=["POST"])
def api_chat_upload():
    """工作台「上传图」→ 一个短命 ref（`cr_…`），不落图库。

    为什么另起一条而不是复用画廊上传：工作台是聊天优先的界面，用户随手贴
    一张图只是「这一轮要照它改」，落进作品墙反而会把他的画库塞满（2026-11-26
    查证：工作台现在的参考图走 `/api/gal/ref/*`，是画廊那套私有参考图，需要
    先有作品集）。这条只把图存在内存里，两小时没人用就丢。

    body: `{image: "data:image/..."}`（前端压到长边 ~960px 的缩略图）
    → `{ok: true, ref: "cr_xxx"}`
    """
    from app.features.gallery import references
    data = request.get_json(silent=True) or {}
    image = str(data.get("image") or "").strip()
    if not image.startswith("data:image/"):
        return jsonify({"ok": False, "error": "图片格式不对"}), 400
    if len(image) > (6 << 20):
        return jsonify({"ok": False, "error": "图片太大了"}), 400
    # 访客也能上传参考图（2026-11-26 修）：原先只认 qq，访客会被挡成
    # 「没登录」。图片不落图库，只在这个进程内存里挂两小时，跟 qq 无关——
    # 归属键换成「qq 或访客身份段」，换浏览器也读不到别人的那份。
    owner = _who_qq_or_401() or _guest_key()
    if not owner:
        return jsonify({"ok": False, "error": "没登录"}), 401
    ref = references.new_chat_ref()
    now = time.time()
    with _WEB_IMG_LOCK:
        mine = [(k, v) for k, v in _WEB_IMG_CACHE.items()
                if v.get("qq") == owner]
        if len(mine) >= _WEB_IMG_MAX:
            mine.sort(key=lambda kv: kv[1]["at"])
            for k, _v in mine[:len(mine) - _WEB_IMG_MAX + 1]:
                _WEB_IMG_CACHE.pop(k, None)
        _WEB_IMG_CACHE[ref] = {"qq": owner, "data": image, "at": now}
    return jsonify({"ok": True, "ref": ref})


def _who_qq_or_401():
    from app.features.chat import routes as chat_routes
    return chat_routes._who() or None


def _purge_web_images():
    """丢掉两小时没动过的临时参考图（`chat-upload` 的回收）。"""
    cut = time.time() - _WEB_IMG_TTL
    with _WEB_IMG_LOCK:
        for k in [k for k, v in _WEB_IMG_CACHE.items() if v["at"] < cut]:
            _WEB_IMG_CACHE.pop(k, None)


@bp.route("/api/gen/submit", methods=["POST"])
def api_submit():
    """提交一张图：校验 → 起 worker 线程 → 返回任务 id（前端轮询 status）。"""
    data = request.get_json(silent=True) or {}
    skill = str(data.get("skill") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    artist_prefix = str(data.get("artist_prefix") or "").strip()
    if skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r（刷新页面重载渠道列）"
                        % skill}), 400
    if skill not in image_jobs.NAI_SKILLS:
        artist_prefix = ""
    if len(artist_prefix) > _NAI_PREFIX_MAX:
        return jsonify({"ok": False,
                        "error": "画师串太长了（%d 字，上限 %d）"
                                 % (len(artist_prefix), _NAI_PREFIX_MAX)}), 400
    prompt = _nai_prompt(prompt, artist_prefix)
    if not prompt:
        return jsonify({"ok": False, "error": "提示词是空的"}), 400
    # 中文原样进 CLIP = 等于没给提示词（见 `_translate_prompt` 那段注释）。
    # 放进长度校验之前：翻完可能变长，得按最终串判上限。
    if data.get("prompt_is_final"):
        data["prompt_translated"] = False
    elif _needs_translate(skill, prompt):
        en = _translate_prompt(skill, prompt)
        if en:
            prompt = en
            data["prompt_translated"] = True
    if len(prompt) > _MAX_PROMPT:
        return jsonify({"ok": False,
                        "error": "提示词太长了（%d 字，上限 %d）"
                                 % (len(prompt), _MAX_PROMPT)}), 400
    # LoRA 槽位：不传 = 照模板；传了先校验（错误在提交阶段就退，别等出图线程炸）
    slots, errs, _warn = lora_slots.normalize(data.get("loras"))
    if errs:
        return jsonify({"ok": False, "error": "；".join(errs)}), 400
    # 自定义步数（2026-10-29）：不传/空 = 按渠道模板。NAI 是云端参数，本地步数
    # 没有意义，直接忽略（前端那边也把输入框藏了）。
    steps, step_err = gen_steps.normalize(data.get("steps"))
    if step_err:
        return jsonify({"ok": False, "error": step_err}), 400
    # 自定义 CFG / 负向（2026-10-31）：同样「不传/空 = 按渠道模板」。
    # 负向只做**追加**（模板负向池保留），空串等于没填。NAI 是云端参数，
    # 本地这两个都不认，一并清掉（前端也把输入框藏了）。
    cfg, cfg_err = gen_params.normalize_cfg(data.get("cfg"))
    if cfg_err:
        return jsonify({"ok": False, "error": cfg_err}), 400
    negative, neg_err = gen_params.normalize_negative(data.get("negative"))
    if neg_err:
        return jsonify({"ok": False, "error": neg_err}), 400
    # 底图尺寸（2026-10-31）：只在 moss 渠道受理——别的渠道带放大链，
    # 改底图会让最终像素和填写值对不上（见 `_size_editable`）。老页面
    # 或手搓请求带着 width 打过来，忽略并记一条，不报错也不生效。
    size, size_err, size_note = gen_params.normalize_size(data.get("width"),
                                                          data.get("height"))
    if size_err:
        return jsonify({"ok": False, "error": size_err}), 400
    if size and size_note:
        log.info("跑图台 %s 尺寸规整：%s", skill, size_note)
    if size and not _size_editable(skill):
        log.info("跑图台：渠道 %s 不开尺寸自定义，忽略 %d×%d", skill, *size)
        size = None
    # 横屏（2026-11-10）：独立于「自定义参数」的一键开关，所有 ComfyUI
    # 渠道都受理（按画布节点认，新增渠道自动生效）。NAI 云端已有 nai_wide
    # 渠道，这里忽略，免得用户以为勾了能改云端尺寸。
    landscape = bool(data.get("landscape")) and skill not in image_jobs.NAI_SKILLS
    if landscape and size:
        # 两处都改同一个画布节点，横屏优先；不让手填值把它悄悄覆盖回去。
        log.info("跑图台：勾了横屏，忽略手填尺寸 %d×%d", *size)
        size = None
    if skill in image_jobs.NAI_SKILLS:
        steps = None
        cfg = None
        negative = None
        size = None
    # 槽位补文件名：前端只传槽号，账本行要靠名字显示「这张图挂了哪个 LoRA」
    if slots:
        wf = (load_skill(skill) or {}).get("workflow") or {}
        if lora_slots.has_toggle(wf):
            names = {s["slot"]: s.get("name") or ""
                     for s in lora_slots.read_slots(wf)}
            for s in slots:
                s["name"] = names.get(s["slot"], "")
    # 参考图（2026-11-23）：把图读成描述再拼进提示词，不做像素级的图生图。
    # 放在这里拼是因为后面紧跟的就是长度校验与建 job，拼完超长会被一起管住。
    # 读图失败不拦提交：图读不出来不该让用户整条消息白写——明确说一声、按
    # 纯文字出图，并把这件事记在 job 上让前端显示出来。
    ref_ids, ref_note = _ref_ids(data.get("refs"))
    # 「画同款」已经把这张读成描述填进输入框了，用户点发送时那句话里就有
    # 它。再看一次图不只是白花一次额度，还会把同一段描述**又拼一遍**
    # （提示词里出现两份），2026-11-24 用户确认的口径：前端带了
    # `ref_described` 就说明读过了，不再识图。
    if data.get("ref_described"):
        ref_ids = []
        ref_note = ""
    if ref_ids:
        ref_text = _describe_refs(ref_ids)
        if ref_text:
            prompt = "%s\n\n画面参考（来自用户上传的图，请照着画）：%s" % (
                prompt, ref_text)
            ref_note = "已带上 %d 张参考图" % len(ref_ids)
        else:
            ref_note = "参考图没读出来，这张按你的描述来"
    if not prompt:
        return jsonify({"ok": False, "error": "提示词是空的"}), 400
    if len(prompt) > _MAX_PROMPT:
        return jsonify({"ok": False,
                        "error": "提示词太长了（%d 字，上限 %d）"
                                 % (len(prompt), _MAX_PROMPT)}), 400
    # 5175 网关在 query/body 注入 qq；5174 本机管理页则由 chat routes
    # 使用同一套站主身份判定。无身份时退化为单一匿名档，避免绕过限流。
    # 这里不能 `from ...chat.routes import _who`：image 包比 chat 包先加载，
    # 那样会变成循环导入（只判"有没有拿到档位"，判断权仍归 chat 那套）。
    from app.features.chat import routes as chat_routes
    # 访客（2026-11-26）：跟 `api_ask` 同一套身份口径。原先这里是
    # `_who() or "__anonymous__"`，于是**所有访客共用一条任务线**：认领
    # （adopt）时按 owner 找不到自己那张，兜底写档也会写到别人那一档去。
    # 访客的身份头只有工作台发来的请求才有，跑图台照旧无副作用。
    owner = chat_routes._owner() or "__anonymous__"
    # 「同步发 QQ」（2026-11-27）：重画 / 画同款 / 换种子重画都走 submit。
    # 前端开关在这里也要读，不然勾着发的图只有第一张（ask）会到 QQ。
    # qq 只认 `chat_routes._who()`——网关从 cookie 注入的那个，不信请求体。
    _sync = bool(data.get("qq_sync")) and bool(chat_routes._who())
    job, busy = _enqueue_job({
        "skill": skill, "prompt": prompt, "loras": slots,
        "steps": steps, "cfg": cfg, "negative": negative, "ref_note": ref_note,
        "width": size[0] if size else None,
        "height": size[1] if size else None,
        "landscape": landscape,
        "rec": str(data.get("rec") or "").strip(),
        # 开关关着就写空串：`_sync_to_qq` 见到空 qq 直接返回。
        "qq_sync": _sync,
        "qq": (chat_routes._who() or "") if _sync else "",
    }, owner=owner)
    if job is None:
        return jsonify({"ok": False, "busy": True, "error": busy}), 429
    return jsonify({"ok": True, "job": _public(job)})


def _enqueue_job(spec, owner, user_text="", lowres_on=False):
    """建一条本台任务并起 worker。→ (job, None) 成功；→ (None, 一句人话) 没名额。

    `api_submit`（老路：翻译后直通）与 `api_ask`（工作台一句话，走 QQ 那份
    转译）**共用这一段**：两条路最后都是「一张确定渠道 + 确定提示词的图」，
    限额、记账、终止信号、轮询都不该有两份实现。

    `lowres_on`：这条任务要不要走「工作台低分辨率档」（`image/lowres.py`）。
    两条入口共用 worker，所以这一档**按任务标**，不是全局开关——本地跑图台
    `/gen` 自己用、要原图，工作台那边的图出站前本来就要压到 ≤1MB。

    spec 只收两边都有的字段；缺省按 None / "" 落（LoRA 槽位、步数、CFG 这些
    只有 `api_submit` 会带，工作台那条路全是默认值）。
    """
    limit, _peak_label, _window = image_jobs.inflight_limit()
    skill = spec["skill"]
    with _JOBS_LOCK:
        # 本台独立 worker 线程与公共 NAI 队列分别计数，避免重复计同一任务。
        active = sum(1 for j in _JOBS.values()
                     if j.get("owner") == owner
                     and j["state"] in ("queued", "running"))
        if active >= limit:
            # 界面上一句话就够（前端只闪这一行），不带喂给模型的指令。
            # 2026-11-25：高峰期那档删掉，任何时间都是「最多同时 5 张」。
            return None, "同时最多画 %d 张，等一张出来再画。" % limit
        job = {"id": _new_job_id(), "owner": owner,
               # 这条任务对应**哪个聊天记录**（2026-11-26 服务端兜底写档用）。
               # 前端提交时带上自己的记录 id；「直达」那几条拿不到（记录是回执
               # 之后才建的），所以写档时还会退回按任务号认，见 chat.routes.write_back。
               "rec": str(spec.get("rec") or ""),
               "skill": skill, "prompt": spec["prompt"],
               "seed": (None if skill in image_jobs.NAI_SKILLS
                        else random.randint(0, 2 ** 31 - 1)),
               "loras": spec.get("loras") or [],
               "steps": spec.get("steps"), "steps_label": "",
               "cfg": spec.get("cfg"), "negative": spec.get("negative"),
               "width": spec.get("width"), "height": spec.get("height"),
               "landscape": bool(spec.get("landscape")),
               # 工作台低分辨率档：这条任务出图前画布降一档、倍率补回来。
               "lowres": bool(lowres_on),
               # 「同步发 QQ」（2026-11-27）：出图后往 `qq` 的私聊再发一份。
               # 只有工作台登录用户会带上来，跑图台 / QQ 那几个入口没有这两
               # 个键，`_sync_to_qq` 直接返回。qq 不下发前端（见 `_public`）。
               "qq_sync": bool(spec.get("qq_sync")),
               "qq": str(spec.get("qq") or ""),
               "size_label": "",
               "ref_note": spec.get("ref_note") or "",
               # 图生图（2026-11-26 工作台）：模型判「要垫这张图」时由
               # `_web_decide` 塞进来的 data URL 列表。有就走 i2i 骨架，
               # 没有就走文生图——跟 QQ 那边同一个判据（`generate_image`
               # 里 `is_i2i = bool(source_image)`）。
               "source_images": list(spec.get("source_images") or []),
               "ask": user_text,
               "secs": None,
               "state": "queued", "error": "", "file": "",
               "ts": time.time(),
               "_thread_alive": True,     # 见 _reap_stuck
               # 中断信号（2026-11-04 跑图台「中断」按钮）：
               # 置位后排队中的由 image_jobs worker 跳过、正在跑的由
               # wait_done 走**定向** /interrupt（只停这一张，不误伤 QQ 的图）。
               "abort": threading.Event()}
        _JOBS[job["id"]] = job
        # 只留最近 _JOB_KEEP 个，防长时间不刷新把内存堆起来
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["ts"])[:-_JOB_KEEP]:
            if _JOBS[old]["state"] != "queued":
                _JOBS.pop(old, None)
    # 记录里**从第一秒就有任务号**（2026-11-27 用户实测：9:55:34 提交可莉，
    # 大退重进来只有一句「这条没跑完就断了」，可图早就画完了）。
    # 提交那一发要等模型判意图，慢的时候几十秒；用户在这个窗口里大退，响应就
    # 丢了，`rec.job` 永远是空。以前只有「任务结束」那一刻才写档，于是记录里
    # 有 file、没有 job ——重进既没法按 job 查状态，adopt 又只认还在跑的，只能
    # 判死。这里先落一次，重进直接按任务号查，done 就出图。
    # 写不进去（记录还没推上来）也不要紧：adopt 那边有按记录 id 精确认领的一条。
    _archive_back(job, {"job": job["id"], "err": False,
                        "note": "正在跑，画好会自动弹出来"})
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job, None


def _ref_ids(raw):
    """保留有效的私有 ref_id 和旧正整数 item_id。返回 (ids, 空串)。"""
    if not isinstance(raw, (list, tuple)):
        return [], ""
    from app.features.gallery import references

    ids = []
    for x in raw[:MAX_REFS]:
        if isinstance(x, str) and references.valid_ref_id(x):
            if x not in ids:
                ids.append(x)
            continue
        if isinstance(x, bool) or not isinstance(x, (int, str)):
            continue
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in ids:
            ids.append(i)
    return ids, ""


def _describe_refs(ids):
    """读取私有 ref_id 或旧图库 item_id 的描述，跳过失效引用。"""
    from app.features.gallery.routes import (
        DESCRIBE_PROMPT, _read_image_text, _read_private_image_text)

    texts = []
    for ref in ids:
        if isinstance(ref, str):
            t = _read_private_image_text(ref, DESCRIBE_PROMPT)
        else:
            t = _read_image_text(ref, DESCRIBE_PROMPT)
        if t:
            texts.append(t)
    if not texts:
        return ""
    if len(texts) == 1:
        return texts[0]
    return "；".join("参考图%d：%s" % (n, t) for n, t in enumerate(texts, 1))


def _new_job_id():
    """GP-YYYYMMDD-HHMMSS-毫秒。和 image_log 的编号同构（时间有序、全数字后缀），
    但前缀不同——两边账本各记各的，号撞了也不是同一张图。"""
    t = time.time()
    st = time.localtime(t)
    ms = int(round((t - int(t)) * 1000)) % 1000
    return "GP-%s-%s-%03d" % (time.strftime("%Y%m%d", st),
                              time.strftime("%H%M%S", st), ms)


# ─── 卡死兜底（2026-11-24 用户：「昨天晚上的图现在还显示排队中，卡死了」）────
#
# 前端现在自己计时（见 chat.js 的 `SETTLE_MS`），但它只认得见的那一张——
# 用户关掉页面、换个设备再看，存档里那条还是「排队中…」。服务重启之后
# `_JOBS` 是空的，前端也就再也查不到它的下落。所以在服务端也补一层：
# **worker 线程起不来 / 被吞掉**这种状态，超时后明确判成「没跑成」，
# 而不是让一条记录永远挂着。
#
# 判据只用时间：从提交到现在超过「排队上限 + 单张上限」就算。既不用
# ComfyUI 探活（探不到不等于死了），也不去猜提示词——只报事实。
#
# 上限按渠道取（qwen 系 420 秒是既有配置，见 jobs.SKILL_TIMEOUTS），
# 再留出排队与提交前等待的余量。
_QUEUE_SLACK = 15 * 60


def _stuck_after(skill):
    try:
        from app.features.image import jobs as _jobs
        per = float(_jobs.task_timeout(skill) or _jobs.TASK_TIMEOUT)
    except Exception:                        # noqa: BLE001
        per = 180.0
    return per + _QUEUE_SLACK


def _reap_stuck():
    """把「worker 线程已经没了、记录却还是 queued」的任务标成失败。

    纯事实判断：不用 ComfyUI 探活（探不到不等于死了），不猜提示词，也不
    凭空超时——画得慢但还在跑的图不能被杀。这里只回答一个问题：**这张
    图还有人在等它吗？** 没有就是它再也不会有结果，明说。
    """
    dead = []
    now = time.time()
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.get("state") != "queued":
                continue
            if job.get("_thread_alive", True):
                continue
            dead.append(job)
        for job in dead:
            job["state"] = "error"
            job["error"] = "这一张没跑起来（服务那边丢了它的线程），点重试再来"
            job["stuck"] = True
    if dead:
        log.warning("跑图台收掉 %d 张卡住的任务：%s 已挂 %.0f 秒",
                    len(dead), ", ".join(j["id"] for j in dead),
                    now - float(dead[0].get("ts") or now))
    return len(dead)


@bp.route("/api/gen/status")
def api_status():
    """进行中/近期任务 + ComfyUI 队列水深（含 QQ 那边的，好解释为什么慢）。

    `phase`（2026-11-23）是这一张**现在到底在哪一段**，前端不用再猜「画图中」：
      · queued  —— 还在我们自己的队列里，前面还有 N 张；
      · submit  —— 已被 worker 取走，正在装模型 / 等 ComfyUI，还没交出去；
      · running —— ComfyUI 手里确有这个 prompt_id，正在采样；
      · unknown —— **拿不到准信**：ComfyUI 探不到（正在重启），或者两边都没
                   查到但 worker 线程还活着。跟「不知道就说不知道」同一条：
                   这一格只回答「我们不知道它此刻在哪」，不替 ComfyUI 判死。
      · lost    —— **确实没了**：ComfyUI 探得到、队列和 history 都没有它，
                   并且 **worker 线程已经不在**（2026-11-26 用户拍板）。
                   以前只要探不到就给 lost，把正在被 `wait_comfy_up` 耐心等待
                   的图当场判死（实测 21:58:00 掉线、21:58:06 回来、
                   21:58:40 正常出图），用户被引去重排，白跑一张。
    探针只问**本台正在跑的那一张**，最多 1 次 /queue，不会因为状态轮询把
    ComfyUI 打爆；问不到（超时、离线）返回 None，前端按「探不到」处理。
    """
    _reap_stuck()
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j["ts"], reverse=True)
    # 访客只看得见自己的（2026-11-26）：原来这一条不过滤 owner，整台机器的
    # 任务（包括站主在 QQ 那边发起的）都会吐给任何访客，任务标题就是提示词。
    # **只对访客收口**：登录用户和站主本机保持原样（跑图台、管理页都用同一
    # 条接口看全量队列，那里不能变）。
    # 登录用户身上就算还挂着旧的访客证，也不能按访客收口——他自己的任务
    # owner 是 qq，按 `guest_<id>` 筛会把自己所有任务都滤掉（2026-11-26）。
    _mine = "" if _who_qq() else _guest_key()
    if _mine:
        jobs = [j for j in jobs if j.get("owner") == _mine]
    try:
        depth = image_jobs.queue_depth()
    except Exception:
        depth = None
    # 这一轮里本地通道真正在跑的那张（tag → 位置），一次查清所有 job
    try:
        snap = image_jobs.snapshot()
    except Exception:
        snap = {}
    _run = snap.get("running") or {}
    running_tag = str(_run.get("tag") or "")
    running_pid = str(_run.get("pid") or "")
    queued_tags = {q.get("tag") for q in (snap.get("queued") or [])}
    _comfy_up = None                     # 三态：None=还没探 / True / False

    def _probe_up():
        """问一次 ComfyUI 在不在；一轮里只问一次（状态页 2 秒轮询一次）。"""
        nonlocal _comfy_up
        if _comfy_up is None:
            try:
                _comfy_up = image_jobs._comfy_up()
            except Exception:            # noqa: BLE001
                _comfy_up = None
        return _comfy_up

    def _phase_of(job):
        """这一张现在到底在哪一段（见 docstring 那几段）。

        `lost` 只留一个出口：**worker 线程已经没了**（2026-11-26 用户拍板）。
        探不到 ComfyUI 不等于这张没了——worker 那边 `wait_comfy_up` 正在等它
        回来（上限 180 秒），ComfyUI 起来后它自己的队列还会把这张接着跑完
        （实测 21:58:00 掉线、21:58:06 回来、21:58:40 出图）。漫画台走的就是
        `wait_comfy_up` 那一套，这里跟它对齐：拿不到准信就给 `unknown`。
        """
        jid = job["id"]
        pid = str(job.get("pid") or "")
        if jid in queued_tags:
            return "queued"
        # 「本地通道真的有一张在跑」且就是我：worker 已经取走，还没交给 ComfyUI。
        # ⚠️ 两边都为空**不算**对得上——否则所有没入队信息的老 job 都会被判成
        # 正在提交，下面那条探测就永远走不到。
        if jid == running_tag or (running_tag and running_pid and pid == running_pid):
            return "submit"
        if not pid:
            return "submit"
        # 探不到 ComfyUI：它大概率在重启。不知道就只说不知道。
        if _probe_up() is not True:
            return "unknown"
        # 队列里有它 = 还排着；history 里有它 = 刚画完、回包还没到。都算 running。
        if image_jobs._comfy_queue_has(pid) is not False:
            return "running"
        if image_jobs.poll_once(pid) is not None:
            return "running"
        # 两边都查不到，但 worker 线程还在 = 它在等重启 / 正在重试提交，别判死
        if job.get("_thread_alive", True):
            return "unknown"
        return "lost"

    for job in jobs:
        # phase / ahead 对**所有还没结束的**都算（2026-11-27 实测）：原来只算
        # queued，worker 一取走 state 就变 running，`ahead` 字段整块消失，
        # 前端 `typeof job.ahead === "number"` 为假、当成 0 —— 胶囊当场掉回
        # 灰字（用户：「刚才还是胶囊，怎么又变灰了」）。running 时它就是 0，
        # 字段一直在，外观只随文案推进，不跳。
        if job["state"] not in ("queued", "running"):
            continue
        job["phase"] = _phase_of(job)
        # 「前面还有几张」：前端拿它把「正在提交」说成「前面还有 3 张」
        # （2026-11-27）。后台算，因为只有队列那边知道谁排在谁前面。
        job["ahead"] = image_jobs.ahead_by_tag(job["id"], job.get("skill"))
    return jsonify({"ok": True, "jobs": [_public(j) for j in jobs],
                    "queue_depth": depth, "comfy_online": _comfy_up})


@bp.route("/api/gen/adopt")
def api_adopt():
    """按**记录 id** 或**提交时间**认领一条已经排上的任务（刷新页面时用）。

    「点发送 → 页面立刻刷新」这种最普通的操作，前端会丢一次：任务号是
    提交接口响应回来之后才写进记录的，页面在那一刻刷新，存档里那条就只剩
    「有提示词、没任务号、没图」。以前前端只能把它判成「这条没跑完就断了」
    ——可服务端明明还排着队（用户 2026-11-25 报的正是这句）。

    认人有两条路，**先精确后模糊**：

    ① `rec`（推荐）：任务建出来时就把记录 id 记在 `job["rec"]` 上，拿它
       查是确定的，任何状态都回（含 done / cancelled / error）。为什么连
       已完成的也回：用户 2026-11-27 实测「提交可莉 → 大退 → 重进」，那条
       记录里没有 file、没有 job，而图其实已经画好了；只回还在跑的就只能
       给他一句「这条没跑完就断了」。回 done + file，重放那一侧直接铺图。

    ② `at`（老路，兼容）：按提交时间 ± `ADOPT_WINDOW` 秒、同一 owner、
       只认还没结束的（queued / running）——时间不像 id 那样唯一，多命中
       时取最近的那条，所以不敢回 finished 的，怕认错邻居那张。
    """
    try:
        at = float(request.args.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    rec = str(request.args.get("rec") or "").strip()
    if at <= 0 and not rec:
        return jsonify({"ok": False, "error": "没给时间"}), 400
    from app.features.chat import routes as chat_routes
    # 和提交同一口径（2026-11-26）：`_owner()` = qq，没有就是工作台访客的
    # `guest_<身份段>`。原先这里是 `_who()`，访客恒为空 → 落到 `__anonymous__`，
    # 而提交那边记的是 `guest_xxx`，两边永远对不上，访客刷新后认不回自己那张。
    owner = chat_routes._owner() or "__anonymous__"
    # ① 按记录 id 认（2026-11-27）：任务建出来时就把 `rec` 记在 job 上了，
    #    拿它认是**确定**的，比时间窗准，不会认错邻居那张。已完成 / 已终止
    #    的也照回——「提交到一半大退」的那条记录里没有 file，全靠这里把它
    #    接回那张**其实已经画好**的图（原来只回还在跑的，于是重进只能看到
    #    「这条没跑完就断了」，可图在盘上躺着）。
    if rec:
        with _JOBS_LOCK:
            for job in _JOBS.values():
                if job.get("rec") == rec and job.get("owner") == owner:
                    return jsonify({"ok": True, "job": _public(job)})
    best = None
    with _JOBS_LOCK:
        for job in _JOBS.values():
            if job.get("owner") != owner:
                continue
            if job.get("state") not in ("queued", "running"):
                continue
            gap = abs(float(job.get("ts") or 0) - at / 1000.0)
            if gap > ADOPT_WINDOW:
                continue
            if best is None or gap < best[0]:
                best = (gap, job)
    if best is None:
        return jsonify({"ok": True, "job": None})
    return jsonify({"ok": True, "job": _public(best[1])})


@bp.route("/api/gen/cancel", methods=["POST"])
def api_cancel():
    """中断这一张（2026-11-04 用户要的「中断任务」按钮）。

    只认本台自己发出去、还没结束的任务：置位 job 的中断 Event，
    正在跑的那张由 `image_jobs` 带 prompt_id 做**定向** /interrupt
    （不会像全局 /interrupt 那样把 QQ 的图一起劈掉），排队中的直接跳过。

    2026-11-23 修：原来只置位本台这份 Event，而 `image_jobs.process` 提交给
    ComfyUI 那一段（装模型 / 等上线）根本不看它，所以「点了终止还在画完」。
    现在除了置位本台的 Event，还按 **tag = 任务号** 去 `image_jobs` 里把真正的
    job 标成 stopped——worker 会在提交前 / 投递前收手，已经在采样的由它定向
    掐掉并等退场。
    """
    data = request.get_json(silent=True) or {}
    job_id = str(data.get("id") or "").strip()
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "找不到这个任务（刷新页面重试）"}), 404
        if job["state"] != "queued":
            return jsonify({"ok": False, "error": "这张已经结束了"}), 400
        job["abort"].set()
        job["state"] = "cancelled"
        job["error"] = "已中断"
    hit = False
    try:
        hit = image_jobs.cancel_tag(job_id, "用户终止了这张图")
    except Exception:
        log.warning("跑图台 %s：掐 image_jobs 里那张失败", job_id, exc_info=True)
    log.info("跑图台 %s 收到终止请求（image_jobs 命中=%s）", job_id, hit)
    return jsonify({"ok": True, "hit": hit,
                    "message": "已终止" if hit else "已停下，这张没有在画"})


@bp.route("/api/gen/history")
def api_history():
    limit = request.args.get("limit") or 100
    return jsonify({"ok": True, "rows": _ledger_rows(limit)})


@bp.route("/api/gen/clear", methods=["POST"])
def api_clear():
    """清空历史显示：记截止时间戳。文件和账本行都不动（见模块顶部约定）。"""
    try:
        d = os.path.dirname(_CLEARED_AT)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(_CLEARED_AT, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except OSError as e:
        return jsonify({"ok": False, "error": "写不进去：%s" % e}), 500
    return jsonify({"ok": True})


@bp.route("/api/gen/imgsign")
def api_imgsign():
    """给图片地址签一个短时有效的 token（2026-11-24）。

    ## 为什么要它

    群友拿到的是 Cloudflare 临时隧道地址，**隧道一重连就换域名**
    （`xxxx.trycloudflare.com` → `yyyy.trycloudflare.com`）。浏览器缓存是按
    完整 URL 存的，域名一变，昨天存下的所有图全部作废——这就是用户说的
    「我每次重新进入这个工作台，图片好像不是从缓存里面拿的」。

    前端因此要把图片地址里的域名拿掉：只记**相对路径 + token**，渲染时用
    `当前页面的 origin + 相对路径` 拼回去。这样换域名后地址不变，缓存照命中。
    token 是为了让「不带登录 cookie 也能取图」——本地缓存那套（`wsimg.js`）
    用 `no-cors` 请求图片字节时带不了凭据，而 5175 网关对 `/api/gen/img` 本身
    不拦（读接口放行），拦的只是 5174 的 token 校验。

    token = HMAC(secret, "imgsign")，只用来证明「这个请求是本站页面发起的」，
    不带任何身份——所以不需要过期时间，也不需要存盘。
    """
    import hashlib
    import hmac
    from app.gateways import friend_auth
    try:
        secret = friend_auth.secret()
    except Exception:                            # noqa: BLE001
        return jsonify({"ok": False, "error": "服务未就绪"}), 503
    if not secret:                               # 密钥还没生成：别签一个空串出去
        return jsonify({"ok": False, "error": "服务未就绪"}), 503
    sig = hmac.new(str(secret).encode("utf-8"), b"gen:img:1",
                   hashlib.sha256).hexdigest()[:32]
    return jsonify({"ok": True, "token": sig})


def _img_sign_ok(raw):
    """校验 `?k=`（前端带来的短时 token）。"""
    got = str(raw or "").strip()
    if not got:
        return False
    try:
        import hashlib
        import hmac
        from app.gateways import friend_auth
        secret = friend_auth.secret()
    except Exception:                            # noqa: BLE001
        return False
    if not secret:
        return False
    want = hmac.new(str(secret).encode("utf-8"), b"gen:img:1",
                    hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(got, want)


@bp.route("/api/gen/img")
def api_img():
    """按图片引用取图：`nai:<filename>` 走 NAI 临时目录，其余走 ComfyUI。

    两条路径都只接受文件名/受控相对路径，不接受绝对路径；NAI 的绝对落盘路径
    永远不会下发给浏览器。

    ## 缓存（2026-11-23 用户：「都说了 localStorage 去存储，不要让服务器老传」）

    出图路径里带文件夹名，**文件名就是唯一的**（ComfyUI 的 `ComfyUI_00042_.png`
    在同一目录下不会重名），下发给浏览器的那条地址永远不会变内容。所以这里
    直接给长缓存 + `immutable`，配合带 `If-None-Match` 的请求还能拿 304：
    群友手机重新打开页面时，图上一次已经躺在浏览器缓存里，一个字节都不用
    再过隧道——这是「服务器带宽老被图片吃掉」里最直接的省法。

    以前写的是 `no-store`，等于每刷一次页面所有图都从 origin 重下一遍，
    走 Cloudflare 隧道时就是美国绕一圈回来。

    ## 两个新参数（2026-11-24）

    · `fit=512k` —— 发「长边试到 ≤512KB」的那一档 WebP，工作区的下载和
      列表都用它（和画廊一个量级）。见 `_img_fit_file`。
    · `k=<token>` —— 站点签名（`/api/gen/imgsign` 下发）。前端把图存本地缓存
      时要自己取一次字节，那条路带不了内部令牌，所以**只在这条路由上**额外
      认它：有它就和带内部令牌等价。没带的老请求照旧走 `_guard()`。
    """
    f = str(request.args.get("f") or "").replace("\\", "/").strip("/")
    # `k=` 只在「本台取图」这一条路上被认可：它不是登录凭据，也调不动别的
    # 接口，只证明「这个请求是本站页面发的」。有它就当内部请求放行；没有就
    # 按老规矩走 `_guard()`（5175 带的内部令牌 / 本机直连免检）。
    if not _img_sign_ok(request.args.get("k")):
        from app.features.gallery import routes as _gal
        bad = _gal._guard()
        if bad:
            return bad
    if f.startswith("nai:"):
        name = f[4:]
        if not _NAI_REF_RE.fullmatch(f) or not _IMG_NAME_RE.fullmatch(name):
            return jsonify({"ok": False, "error": "非法文件名"}), 400
        try:
            # send_from_directory 自带目录约束；这里只传 basename，避免路径穿越。
            # conditional=True 才会按 ETag 回 304（见上面 docstring 的缓存说明）
            resp = send_from_directory(image_out._out_dir(), name,
                                       mimetype="image/png", conditional=True,
                                       max_age=_IMG_MAX_AGE)
            resp.headers["Cache-Control"] = _IMG_CACHE_CTL
            return resp
        except Exception:
            return jsonify({"ok": False,
                            "error": "NAI 没有这张图（临时文件已过期？）"}), 404
    parts = [x for x in f.split("/") if x]
    if not parts or ".." in parts or not _IMG_NAME_RE.fullmatch(parts[-1]):
        return jsonify({"ok": False, "error": "非法文件名"}), 400
    # 先读本机 output 目录（2026-11-10）：ComfyUI 会被 RAM 守卫 / 看门狗
    # 重启，那 20 多秒空窗里 /view 全 404，而 <img> 拿到 404 不会自己重试
    # → 图明明跑好了，页面上却是永久破图。读盘不依赖服务在不在线。
    # 和主站 /api/image 共用同一套路径校验，磁盘上没有才走下面的代理。
    local = comfy_out.local_output_file(f)
    if local:
        # `?w=`（2026-11-24）：出图原图常常 2~8MB PNG，聊天区里铺十几张
        # 就是几十 MB 一路穿过家用上行。带 w 就发长边 w 的 JPEG 缩略图，
        # 一张大约 30~80KB，**小两个数量级**；点开大图仍走不带 w 的原图。
        # 缩略图落 `state/thumb/`（按「原图路径+mtime+w」命名），同尺寸
        # 只算一次；失败就退回原图，缩略图只是优化不是前置条件。
        w = _img_thumb_w(request.args.get("w"))
        fit = _img_fit(request.args.get("fit"))
        if fit:
            # 「存下来看」那一档：和上面同一条路，只是宽度由体积上限挑。
            got = _img_fit_file(local, fit)
            if got:
                return _img_send(got)
        if w:
            thumb = _img_thumb_file(local, w)
            if thumb:
                return _img_send(thumb)
        resp = send_file(local, conditional=True, max_age=_IMG_MAX_AGE)
        resp.headers["Cache-Control"] = _IMG_CACHE_CTL
        return resp
    params = {"filename": parts[-1]}
    if len(parts) > 1:
        params["subfolder"] = "/".join(parts[:-1])
    try:
        resp = _session.get(COMFYUI_URL.rstrip("/") + "/view",
                            params=params, timeout=120)
    except requests.RequestException as e:
        return jsonify({"ok": False,
                        "error": "ComfyUI 不在线，图取不到：%s" % e}), 502
    if resp.status_code != 200 or not resp.content:
        return jsonify({"ok": False,
                        "error": "ComfyUI 没有这张图（output 被清过？）"}), 404
    # 代理这一路没有 ETag，只能给长缓存；同一文件名内容不会变，够用
    out = Response(resp.content, mimetype="image/png")
    out.headers["Cache-Control"] = _IMG_CACHE_CTL
    return out
