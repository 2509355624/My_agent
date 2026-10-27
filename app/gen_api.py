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

from app import comic, image_jobs, image_out
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
_MAX_PROMPT = 4000       # 提示词长度上限（防手滑贴进一整篇小说）

# 账本与「清空截止」（测试进程经 state_path 自动落临时目录，互不污染）。
_LEDGER = state_path("gen_pad.jsonl")
_CLEARED_AT = state_path("gen_pad_cleared_at")
_ledger_lock = threading.Lock()

# ComfyUI 输出文件名：纯文件名（output 目录是平的），挡死一切路径形态。
_IMG_NAME_RE = re.compile(r"[A-Za-z0-9._\- ]{1,120}")
_NAI_REF_RE = re.compile(r"nai:([A-Za-z0-9._\- ]{1,120})")


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
                    "summary": skill_summary(sd.get("skill_md") or ""),
                    "cloud": False})
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
    return {k: job[k] for k in ("id", "skill", "prompt", "seed",
                                "state", "error", "file", "ts")}


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
        else:
            wf = generate_image_build(job["skill"], job["prompt"], job["seed"])
            if wf is None:
                raise RuntimeError("渠道 %s 没有文生图工作流" % job["skill"])
            # _submit_retry 只对超时重试；其余错误一次抛清。
            name = comic._submit_retry(
                lambda: comic._submit(wf, job["skill"], job["prompt"], job["seed"]),
                "[跑图台 %s]" % job["id"])
            file_ref = name
            seed = job["seed"]
        _ledger_append({"tag": job["id"], "skill": job["skill"],
                        "prompt": job["prompt"], "seed": seed,
                        "file": file_ref, "ts": job["ts"], "ok": True})
        with _JOBS_LOCK:
            job["state"] = "done"
            job["file"] = file_ref
            job["seed"] = seed
        log.info("跑图台 %s 完成：%s seed %s", job["id"], job["skill"], seed)
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


@bp.route("/api/gen/submit", methods=["POST"])
def api_submit():
    """提交一张图：校验 → 起 worker 线程 → 返回任务 id（前端轮询 status）。"""
    data = request.get_json(silent=True) or {}
    skill = str(data.get("skill") or "").strip()
    prompt = str(data.get("prompt") or "").strip()
    if skill not in _channel_ids():
        return jsonify({"ok": False, "error": "未知渠道 %r（刷新页面重载渠道列）"
                        % skill}), 400
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
