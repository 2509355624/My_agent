"""手动跑图台（页面 /gen）：没有 AI 的文生图界面。

和漫画工作台的分工：那边是「AI 写剧本 → 渲染」，这边是「人写提示词 → 直接跑」。
渠道清单、提交链路、队列守卫全部复用现成件（见各函数注释），本模块只做
「接住表单 → 起线程 → 记账 → 供图」这一层。

## 存储约定（2026-10-14 用户拍板：「历史栏就是 output 文件夹不是吗」）

- **成图不复制**：ComfyUI 的 output 目录就是所有图的归宿（QQ 机器人跑的也在
  那），`/api/gen/img` 走 `/view` 代理直接serve。本机不留第二份字节——单张
  好几 MB，复制一本历史就是几百 MB 的重复。
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
from flask import Blueprint, Response, jsonify, request, send_from_directory

from app import comic, image_jobs, image_out, lora_slots
from app import gen_steps
from app.config import COMFYUI_URL, NAI_API_KEY, WEB_DIR, state_path
from app.direct_gen import _allowed_skills
from app.skills import load_skill, skill_summary

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

# 账本与「清空截止」（测试进程经 state_path 自动落临时目录，互不污染）。
_LEDGER = state_path("gen_pad.jsonl")
_CLEARED_AT = state_path("gen_pad_cleared_at")
_ledger_lock = threading.Lock()

# ComfyUI 输出文件名：纯文件名（output 目录是平的），挡死一切路径形态。
_IMG_NAME_RE = re.compile(r"[A-Za-z0-9._\- ]{1,120}")
_NAI_REF_RE = re.compile(r"nai:([A-Za-z0-9._\- ]{1,120})")
_NAI_PREFIX_MAX = 2000


def _nai_prompt(prompt, prefix):
    """把跑图台的 NAI 画师串放在主体提示词前；其它渠道不经过这里。"""
    prompt = str(prompt or "").strip()
    prefix = str(prefix or "").strip()
    if not prefix:
        return prompt
    return prefix.rstrip(",，;； \\n") + ", " + prompt


# ─── 渠道清单 ────────────────────────────────────────────

# 列表展示顺序：最常用的排前面，其余按名字。QQ 那边渠道词一堆，这里只挑
# 头部的几个——其余字母序也够找。
_HEAD = ("silver", "silver-hd", "hd_3_clear", "hd_3_curvy", "hd_3_gloss",
         "hd_3_soft", "anima-line", "anima-real", "qwen_image_v1", "qwen-hd",
         "nai", "nai_wide", "cunny", "miao", "jank", "nffa")


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
                    "steps": gen_steps.describe(sd.get("workflow") or {})})
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
                                    "steps", "steps_label", "secs")}


def _enabled_loras(job):
    """这张任务实际启用的 LoRA 槽（只留开的，账本行别堆一堆关着的）。"""
    return [{"slot": s["slot"], "name": s.get("name") or "",
             "strength": s.get("strength")}
            for s in (job.get("loras") or []) if s.get("on")]


def _run(job):
    """worker 线程体：按渠道走 ComfyUI 或 NAI 队列，出图后记账。"""
    try:
        if job["skill"] in image_jobs.NAI_SKILLS:
            queued, reason = image_jobs.enqueue(
                None, None, job["prompt"], skill=job["skill"],
                prompt=job["prompt"], seed=None,
                landscape=(job["skill"] == "nai_wide"))
            if queued is None:
                raise RuntimeError(reason or "NAI 队列拒绝了这张图")
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
            # _submit_retry 只对超时重试；其余错误一次抛清。
            # with_entry=True：拿 history entry 算 ComfyUI 真跑这张图的耗时。
            name, entry = comic._submit_retry(
                lambda: comic._submit(wf, job["skill"], job["prompt"], job["seed"],
                                      with_entry=True),
                "[跑图台 %s]" % job["id"])
            secs = image_jobs.exec_seconds(entry)
            file_ref = name
            seed = job["seed"]
        _ledger_append({"tag": job["id"], "skill": job["skill"],
                        "prompt": job["prompt"], "seed": seed,
                        "file": file_ref, "ts": job["ts"], "ok": True,
                        "steps": steps_label, "secs": secs,
                        "loras": _enabled_loras(job)})
        with _JOBS_LOCK:
            job["state"] = "done"
            job["file"] = file_ref
            job["seed"] = seed
            job["steps_label"] = steps_label
            job["secs"] = secs
        log.info("跑图台 %s 完成：%s seed %s %s 步 ComfyUI 耗时 %s 秒",
                 job["id"], job["skill"], seed, steps_label or "-",
                 "%.1f" % secs if secs is not None else "未知")
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
    """跑图台页面（静态文件在 web/ 下，跟漫画工作台并列）。"""
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
    if skill in image_jobs.NAI_SKILLS:
        steps = None
    # 槽位补文件名：前端只传槽号，账本行要靠名字显示「这张图挂了哪个 LoRA」
    if slots:
        wf = (load_skill(skill) or {}).get("workflow") or {}
        if lora_slots.has_toggle(wf):
            names = {s["slot"]: s.get("name") or ""
                     for s in lora_slots.read_slots(wf)}
            for s in slots:
                s["name"] = names.get(s["slot"], "")
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
               "secs": None,
               "state": "queued", "error": "", "file": "",
               "ts": time.time()}
        _JOBS[job["id"]] = job
        # 只留最近 _JOB_KEEP 个，防长时间不刷新把内存堆起来
        for old in sorted(_JOBS, key=lambda k: _JOBS[k]["ts"])[:-_JOB_KEEP]:
            if _JOBS[old]["state"] != "queued":
                _JOBS.pop(old, None)
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return jsonify({"ok": True, "job": _public(job)})


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
    """进行中/近期任务 + ComfyUI 队列水深（含 QQ 那边的，好解释为什么慢）。"""
    with _JOBS_LOCK:
        jobs = sorted(_JOBS.values(), key=lambda j: j["ts"], reverse=True)
    try:
        depth = image_jobs.queue_depth()
    except Exception:
        depth = None
    return jsonify({"ok": True, "jobs": [_public(j) for j in jobs],
                    "queue_depth": depth})


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
    """
    f = str(request.args.get("f") or "").replace("\\", "/").strip("/")
    if f.startswith("nai:"):
        name = f[4:]
        if not _NAI_REF_RE.fullmatch(f) or not _IMG_NAME_RE.fullmatch(name):
            return jsonify({"ok": False, "error": "非法文件名"}), 400
        try:
            # send_from_directory 自带目录约束；这里只传 basename，避免路径穿越。
            resp = send_from_directory(image_out._out_dir(), name,
                                       mimetype="image/png", conditional=False)
            resp.headers["Cache-Control"] = "no-store"
            return resp
        except Exception:
            return jsonify({"ok": False,
                            "error": "NAI 没有这张图（临时文件已过期？）"}), 404
    parts = [x for x in f.split("/") if x]
    if not parts or ".." in parts or not _IMG_NAME_RE.fullmatch(parts[-1]):
        return jsonify({"ok": False, "error": "非法文件名"}), 400
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
    out = Response(resp.content, mimetype="image/png")
    out.headers["Cache-Control"] = "no-store"
    return out
