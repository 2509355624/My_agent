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
from app.features.image import lora_slots
from app.features.comic import comic
from app.features.image import jobs as image_jobs
from app.features.image import out as image_out
from app.features.image import gen_steps
from app.features.image import gen_params
from app.core.cancel import Cancelled
from app.config import COMFYUI_URL, NAI_API_KEY, WEB_DIR, state_path
from app.features.image.direct_gen import _allowed_skills
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
_MAX_ACTIVE = 5          # 本台同时在跑/排队的上限（image_jobs 每会话在途也是 5）
_JOB_KEEP = 40           # 内存里最多留几个任务记录
_LORA_SAVE_LOCK = threading.Lock()   # 「存为默认」是改模板文件，串行化
# 提示词长度上限（2026-10-30 由 4000 提到 10 万）：这个台是给长稿用的——
# 跑 moss 小漫画的分镜提示词动辄几千字，4000 会直接吃 400 把稿子挡在外面。
# 原来那句「防手滑贴进一整篇小说」的顾虑交给用户自己判断，代码不再替他砍。
_MAX_PROMPT = 100000
# 一次最多认几张参考图。每张都要走一次识图（几秒 + 一份额度），挂十张等于
# 让用户干等半分钟；超过的部分直接忽略，不报错。
MAX_REFS = 3

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


def _img_thumb_w(raw):
    """解析 `?w=`：只认白名单里的宽度，其余一律当「不要缩略图」。"""
    try:
        w = int(str(raw or "0").strip())
    except (TypeError, ValueError):
        return 0
    return w if w in _IMG_THUMB_W else 0


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
                                    "phase", "pid",
                                    "steps", "steps_label", "cfg",
                                    "negative", "width", "height",
                                    "size_label", "secs", "landscape",
                                    "ref_note")}


def _enabled_loras(job):
    """这张任务实际启用的 LoRA 槽（只留开的，账本行别堆一堆关着的）。"""
    return [{"slot": s["slot"], "name": s.get("name") or "",
             "strength": s.get("strength")}
            for s in (job.get("loras") or []) if s.get("on")]


def _run(job):
    """worker 线程体：按渠道走 ComfyUI 或 NAI 队列，出图后记账。"""
    landscape_ok = False
    try:
        if job["skill"] in image_jobs.NAI_SKILLS:
            queued, reason = image_jobs.enqueue(
                None, None, job["prompt"], skill=job["skill"],
                prompt=job["prompt"], seed=None,
                landscape=(job["skill"] == "nai_wide"))
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
            # _submit_retry 只对超时重试；其余错误一次抛清。
            # with_entry=True：拿 history entry 算 ComfyUI 真跑这张图的耗时。
            #
            # 2026-11-23：`_submit` 多收两个关键字（都可选，不传时它自己造）：
            #   · task_tag=本台任务号 —— 让 `image_jobs.cancel_tag` / status
            #     的 phase 判断能对上「这是哪一张」；
            #   · on_pid=回调 —— ComfyUI 一接单就把 prompt_id 回填到本台 job，
            #     `/api/gen/status` 才能去 ComfyUI 那边问「它在不在队列里」。
            #     这就是用户要的「探活」：不确定就明说掉队了，别一直写画图中。
            def _on_pid(pid):
                with _JOBS_LOCK:
                    job["pid"] = pid

            name, entry = comic._submit_retry(
                lambda: comic._submit(wf, job["skill"], job["prompt"], job["seed"],
                                      with_entry=True, abort=job["abort"],
                                      task_tag=job["id"], on_pid=_on_pid),
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
    except Cancelled:
        # 用户点了「中断」：队列里那张已由 wait() 放弃等待、worker 会跳过；
        # 正在画的由 image_jobs 定向 /interrupt 后收工。这里只改状态，不记账、
        # 不写失败——主动停的不算「出图失败」。
        with _JOBS_LOCK:
            job["state"] = "cancelled"
            job["error"] = "已中断"
        log.info("跑图台 %s 已中断", job["id"])
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
    if _needs_translate(skill, prompt):
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
    with _JOBS_LOCK:
        active = sum(1 for j in _JOBS.values() if j["state"] == "queued")
        if active >= _MAX_ACTIVE:
            return jsonify({"ok": False,
                            "error": "已经在跑/排队的还有 %d 张，等一张再发"
                                     % active}), 400
        job = {"id": _new_job_id(), "skill": skill, "prompt": prompt,
               "seed": (None if skill in image_jobs.NAI_SKILLS
                        else random.randint(0, 2 ** 31 - 1)),
               "loras": slots,
               "steps": steps, "steps_label": "",
               "cfg": cfg, "negative": negative,
               "width": size[0] if size else None,
               "height": size[1] if size else None,
               "landscape": landscape,
               "size_label": "",
               "ref_note": ref_note,
               "secs": None,
               "state": "queued", "error": "", "file": "",
               "ts": time.time(),
               # 中断信号（2026-11-04 跑图台「中断」按钮）：
               # 置位后排队中的由 image_jobs worker 跳过、正在跑的由
               # wait_done 走**定向** /interrupt（只停这一张，不误伤 QQ 的图）。
               "abort": threading.Event()}
        _JOBS[job["id"]] = job
        # 只留最近 _JOB_KEEP 个，防长时间不刷新把内存堆起来
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["ts"])[:-_JOB_KEEP]:
            if _JOBS[old]["state"] != "queued":
                _JOBS.pop(old, None)
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return jsonify({"ok": True, "job": _public(job)})


def _ref_ids(raw):
    """前端传来的参考图 id 列表 → 干净的整数列表。返回 (ids, 空串)。

    只做**格式**校验：非整数、超出上限一律丢掉。是不是自己的图，交给读图
    那一步按 /img 同一套权限判（别人私有的图读不出来，就按没参考出图）。
    """
    if not isinstance(raw, (list, tuple)):
        return [], ""
    ids = []
    for x in raw[:MAX_REFS]:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in ids:
            ids.append(i)
    return ids, ""


def _describe_refs(ids):
    """把几张参考图读成描述，拼成一段。读不出来的跳过（不回退、不拦提交）。

    复用画廊那边的读图口径（同一套权限、同一段反推提示词），所以画同款和
    聊天里挂参考图得到的是同一种文字，用户看到的效果一致。
    """
    from app.features.gallery.routes import DESCRIBE_PROMPT, _read_image_text

    texts = []
    for i in ids:
        t = _read_image_text(i, DESCRIBE_PROMPT)
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


@bp.route("/api/gen/status")
def api_status():
    """进行中/近期任务 + ComfyUI 队列水深（含 QQ 那边的，好解释为什么慢）。

    `phase`（2026-11-23）是这一张**现在到底在哪一段**，前端不用再猜「画图中」：
      · queued  —— 还在我们自己的队列里，前面还有 N 张；
      · submit  —— 已被 worker 取走，正在装模型 / 等 ComfyUI，还没交出去；
      · running —— ComfyUI 手里确有这个 prompt_id，正在采样；
      · lost    —— **两边都没有它**（ComfyUI 重启过 / 任务被清掉了）。
                   这一条是用户这次报的核心：「我都重启了，网页还写着画图中，
                   我怎么知道它有没有在画？」——不知道就说不知道，别装作在跑。
    探针只问**本台正在跑的那一张**，最多 1 次 /queue，不会因为状态轮询把
    ComfyUI 打爆；问不到（超时、离线）返回 None，前端按「探不到」处理。
    """
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j["ts"], reverse=True)
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
        """这一张现在到底在哪一段（见 docstring 那四个阶段）。"""
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
        if _probe_up() is False:
            return "lost"
        has = image_jobs._comfy_queue_has(pid)
        # 队列里没有、但 history 有 = 画完了，回包还没到，仍算 running
        if has is False and image_jobs.poll_once(pid) is None:
            return "lost"
        return "running"

    for job in jobs:
        if job["state"] == "queued":
            job["phase"] = _phase_of(job)
    return jsonify({"ok": True, "jobs": [_public(j) for j in jobs],
                    "queue_depth": depth, "comfy_online": _comfy_up})


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
    """
    f = str(request.args.get("f") or "").replace("\\", "/").strip("/")
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
        if w:
            thumb = _img_thumb_file(local, w)
            if thumb:
                # gallery.thumb 产出的是 WebP；mimetype 要跟着变，否则
                # 浏览器按 image/jpeg 解析 WebP 会直接裂图。
                resp = send_file(thumb, mimetype="image/webp",
                                 conditional=True, max_age=_IMG_MAX_AGE)
                resp.headers["Cache-Control"] = _IMG_CACHE_CTL
                return resp
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
