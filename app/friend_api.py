# -*- coding: utf-8 -*-
"""群友页的后端内核（挂在 5174 主进程里，**不直接对公网**）。

## 为什么在这边而不是 5175

5175（`friend_app.py`）只做「登录 + 静态页 + 转发」。生图必须回到 5174，
因为 `image_jobs` 的队列是**进程内**的：5135 那个进程自己 import 一份就等于
另起一条队，群友的图和 QQ 的图会各排各的，用户要的「同一条队、能看到前面
还有几张」就没了。

## 谁能调

只认 `X-Friend-Token` 头（值 = `state/friend_secret`）。5175 转发时带上，
别人不知道这个值。**本机浏览器也调不了**——没有头一律 403。

## 限流（用户 2026-10-30 明确要求「不能让我的电脑炸掉」）

两道闸叠着用，任一道满了都拒：

1. 每人在途 ≤ 2（同一个人连点也只占两个位）
2. 群友合计在途 ≤ 6（不跟 QQ 抢整条队，只占自己那一份）

**不设每日总量上限**（用户 2026-10-30 明确要求）：底下是 `image_jobs` 的
单 worker 串行队，画一张就是一张的 GPU 时间，不会因为排队的人多而并发飙升。
真正会被刷爆的是「提交接口本身」和在途堆积，那两样由上面两道闸 + 应用层
IP 限流挡住，跟「每天几张」无关。

也**刻意不记 `image_quota`**：那是 QQ 私聊的账本，群友页去扣同一个 QQ 号，
等于群友在网页画图会把他自己在 QQ 里的额度吃光。两套体系不要混。

## 图存哪

- ComfyUI 出图落 `output/群友页_<qq>/moss/`（走 enqueue 的 out_dir）
- 出图后**复制一份**到 `state/friend_gallery/<qq>/<tag>.png`，因为主 output
  目录会被定期清理；群友的历史不该跟着一起消失
- 图库总量封顶 `GALLERY_CAP` 字节，超了按最老删
"""

import json
import logging
import os
import re
import shutil
import threading
import time

import requests
from flask import Blueprint, jsonify, request, send_file

from app import friend_auth, image_jobs, lora_presets, lora_slots
from app.config import COMFYUI_OUTPUT_DIR, COMFYUI_URL, state_path
from app.skills import load_skill

log = logging.getLogger("friend_api")

# 取图专用 session：本地 ComfyUI，走系统代理只会绕远路
_SESSION = requests.Session()
_SESSION.trust_env = False

bp = Blueprint("friend", __name__)

# 只开这一条渠道（用户 2026-10-30：「暴露在公网的就只有这个 moss 预设网页」）
SKILL = "moss-qunyou"

PER_INFLIGHT = 2            # 每人在途上限
ALL_INFLIGHT = 6            # 群友合计在途上限
GALLERY_CAP = 1 << 30       # 图库总量 1GB，超了删最老
# 群友页提示词上限（2026-10-30 按用户要求 2000 → 20000）。
# 网页端不过模型、不进文本预算，用户想贴多长的 tag 串都直接进工作流，
# 所以这里只是个防呆：挡住误贴整篇文章那种。真正吃 token 的是 ComfyUI
# 的 CLIPTextEncode，太长了它自己会分块、慢，但不会因此坏掉。
PROMPT_MAX = 20000

_TAG_RE = re.compile(r"^[A-Za-z0-9._\-]{1,80}$")
_IMG_RE = re.compile(r"^[A-Za-z0-9._\- ]{1,120}$")

_HISTORY = state_path("friend_history.jsonl")
_GALLERY = state_path("friend_gallery")

# 进程内任务表：tag -> {"job": Job|None, "state":..., "file":..., "error":...}
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_SUBMIT_LOCK = threading.Lock()      # 串行化「算在途 + 入队」，否则连点能绕过闸


# ─── 公共小件 ────────────────────────────────────────────

def _guard():
    """校验内部令牌。→ None 表示放行，否则是要返回的响应。"""
    try:
        good = friend_auth.secret()
    except Exception:
        log.warning("拿不到内部密钥", exc_info=True)
        return jsonify({"ok": False, "error": "服务未就绪"}), 503
    got = str(request.headers.get("X-Friend-Token") or "")
    if not got or got != good:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _qq_arg():
    """从请求里取 QQ 号（已登录，5175 保证过）。"""
    qq = str(request.args.get("qq") or "").strip()
    if not friend_auth.QQ_RE.match(qq):
        return None
    return qq


def _appender(path, row):
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except OSError:
        log.warning("写不进去 %s", path, exc_info=True)
        return False


def _reader(path, limit=200):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
    except OSError:
        return []
    return rows[-limit:]


def _inflight(qq):
    """这个人在途几张 + 群友合计在途几张。"""
    mine = others = 0
    with _JOBS_LOCK:
        for row in _JOBS.values():
            if row.get("done"):
                continue
            if row.get("qq") == qq:
                mine += 1
            others += 1
    return mine, others


# ─── 图库 ────────────────────────────────────────────────

def _gallery_dir(qq):
    return os.path.join(_GALLERY, str(qq))


def _gallery_size():
    total = 0
    for root, _dirs, files in os.walk(_GALLERY):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                continue
    return total


def _trim_gallery():
    """总量超了就从最老的开始删。删不动就停，不硬撑。"""
    if not os.path.isdir(_GALLERY):
        return
    try:
        if _gallery_size() <= GALLERY_CAP:
            return
    except OSError:
        return
    items = []
    for root, _dirs, files in os.walk(_GALLERY):
        for name in files:
            p = os.path.join(root, name)
            try:
                items.append((os.path.getmtime(p), p))
            except OSError:
                continue
    items.sort()
    freed = 0
    need = _gallery_size() - GALLERY_CAP
    for _mtime, p in items:
        if freed >= need:
            break
        try:
            sz = os.path.getsize(p)
            os.remove(p)
            freed += sz
        except OSError:
            continue
    if freed:
        log.info("群友图库超限，清了 %.1f MB", freed / 1048576)


def _fetch(rel):
    """把 ComfyUI 的一张图取成字节。

    优先直接读磁盘（`COMFYUI_OUTPUT_DIR` 配了的话，本机复制最快），否则走
    `/view`——后者跟跑图台取图是同一条路，ComfyUI 在远端也一样能用。
    不猜「最新一张」：名字来自 history，取不到就如实失败。
    """
    rel = str(rel or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if root:
        p = os.path.join(root, *rel.split("/"))
        if os.path.isfile(p):
            try:
                with open(p, "rb") as f:
                    return f.read()
            except OSError:
                log.warning("读盘取图失败 %s", p, exc_info=True)
    parts = rel.split("/")
    params = {"filename": parts[-1]}
    if len(parts) > 1:
        params["subfolder"] = "/".join(parts[:-1])
    try:
        r = _SESSION.get(COMFYUI_URL.rstrip("/") + "/view", params=params,
                         timeout=120)
    except requests.RequestException:
        log.warning("ComfyUI 取图失败 %s", rel, exc_info=True)
        return None
    if r.status_code != 200 or not r.content:
        return None
    return r.content


def _archive(qq, tag, data):
    """把图写进图库 + 生成缩略图。返回相对文件名或 ""。

    缩略图是必须的：原图 2~11 MB，历史网格一屏十几张，不缩一次刷新就是
    几十上百 MB 上传带宽（家用宽带上行只有几十 Mbps），群友会看到"转圈到
    放弃"。缩到 360px 的 webp 后一张约 30 KB，整页 1 MB 以内。
    """
    if not data:
        return ""
    try:
        d = _gallery_dir(qq)
        os.makedirs(d, exist_ok=True)
        dest = os.path.join(d, tag + ".png")
        with open(dest, "wb") as f:
            f.write(data)
    except OSError:
        log.warning("图库归档失败 %s", qq, exc_info=True)
        return ""
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            im.thumbnail((360, 540))
            im.save(os.path.join(d, tag + ".thumb.webp"), "WEBP",
                    quality=80, method=4)
    except Exception:
        # 缩略图失败不影响出图——前端会回退到原图（onerror）
        log.warning("缩略图生成失败 %s", tag, exc_info=True)
    _trim_gallery()
    return tag + ".png"


# ─── 预设 ────────────────────────────────────────────────
# 读写 / 配额 / 公开池 / `zdy-` 解析全部收在 app/lora_presets.py——
# QQ 侧的 generate_image 要查同一份数据，逻辑放两处迟早会不一致。


# ─── 提交 ────────────────────────────────────────────────

def _slots_or_empty(raw):
    slots, errs, _warn = lora_slots.normalize(raw)
    if errs:
        return None, "；".join(errs)
    return slots, ""


@bp.route("/api/friend/loras")
def api_loras():
    bad = _guard()
    if bad:
        return bad
    try:
        wf = (load_skill(SKILL) or {}).get("workflow") or {}
    except Exception:
        log.warning("读不到 %s 模板", SKILL, exc_info=True)
        return jsonify({"ok": False, "error": "渠道模板读不到"}), 500
    if not lora_slots.has_toggle(wf):
        return jsonify({"ok": False, "error": "这个渠道没有 LoRA 槽"}), 500
    return jsonify({"ok": True, "skill": SKILL,
                    "slots": lora_slots.read_slots(wf),
                    "max": lora_slots.SLOT_MAX,
                    "range": list(lora_slots.STRENGTH_RANGE)})


@bp.route("/api/friend/submit", methods=["POST"])
def api_submit():
    bad = _guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    if not friend_auth.QQ_RE.match(qq):
        return jsonify({"ok": False, "error": "没登录"}), 401
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "提示词是空的"}), 400
    if len(prompt) > PROMPT_MAX:
        return jsonify({"ok": False,
                        "error": "提示词太长了（%d 字，最多 %d）"
                                 % (len(prompt), PROMPT_MAX)}), 400
    slots, err = _slots_or_empty(data.get("loras"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    send_qq = bool(data.get("send_qq", True))

    with _SUBMIT_LOCK:
        mine, total = _inflight(qq)
        if mine >= PER_INFLIGHT:
            return jsonify({"ok": False,
                            "error": "你还有 %d 张没画完，等一等" % mine}), 429
        if total >= ALL_INFLIGHT:
            return jsonify({"ok": False,
                            "error": "现在人有点多（%d 张在排），过会儿再来"
                                     % total}), 429
        from app.tools.normal import generate_image
        seed = __import__("random").randint(0, 2 ** 31 - 1)
        try:
            wf = generate_image.build_t2i_workflow(SKILL, prompt, seed)
        except Exception as e:
            log.warning("构建工作流失败", exc_info=True)
            return jsonify({"ok": False, "error": "生成模板出错：%s" % e}), 500
        if wf is None:
            return jsonify({"ok": False, "error": "这个渠道没有文生图模板"}), 500
        if slots:
            lora_slots.apply_slots(wf, slots)
            if any(isinstance(s, dict) and s.get("slot") == 8 and not s.get("on")
                   for s in slots):
                lora_slots.strip_nai5_prefix(wf)
        # 槽位补文件名：历史里要显示「这张挂了哪个 LoRA」
        if slots:
            names = {s["slot"]: s.get("name") or ""
                     for s in lora_slots.read_slots(wf)}
            for s in slots:
                s["name"] = names.get(s["slot"], "")
        job, reason = image_jobs.enqueue(
            "private", qq, wf, skill=SKILL, prompt=prompt, seed=seed,
            out_dir="群友页_" + qq, quiet=not send_qq)
        if job is None:
            return jsonify({"ok": False, "error": reason or "队列拒收"}), 429
        tag = job.tag
        with _JOBS_LOCK:
            _JOBS[tag] = {"qq": qq, "job": job, "ts": time.time(), "done": False,
                          "state": "queued", "file": "", "error": "",
                          "prompt": prompt, "seed": seed,
                          "loras": [s for s in (slots or []) if s.get("on")]}
    on_loras = [s for s in (slots or []) if s.get("on")]
    threading.Thread(target=_watch, args=(tag, qq, job, on_loras),
                     daemon=True).start()
    log.info("群友页 %s：%s 提交 %s", qq, tag, SKILL)
    return jsonify({"ok": True, "tag": tag, "ahead": image_jobs.ahead_of(job)})


def _watch(tag, qq, job, loras):
    """后台等结果：归档 → 记历史 → 落状态。失败要把额度退回去。"""
    ok, err, rel = False, "", ""
    try:
        entry = job.wait()
        names = image_jobs.output_images(entry, job=job)
        if not names:
            raise RuntimeError("跑完了但没有图")
        data = _fetch(names[0])
        if data is None:
            raise RuntimeError("图取不回来（ComfyUI 没响应）")
        rel = _archive(qq, tag, data)
        if not rel:
            raise RuntimeError("图库归档失败")
        ok = True
    except Exception as e:
        err = str(e) or e.__class__.__name__
    with _JOBS_LOCK:
        row = _JOBS.get(tag)
        if row is not None:
            row.update({"done": True, "state": "done" if ok else "error",
                        "file": rel, "error": err})
    _appender(_HISTORY, {"tag": tag, "qq": qq, "ok": ok, "file": rel,
                         "error": err, "ts": time.time(),
                         # 点历史图「存为预设」要靠这行，所以要跟着落盘
                         "loras": loras})
    if not ok:
        log.warning("群友页 %s 失败：%s", tag, err)


@bp.route("/api/friend/status")
def api_status():
    bad = _guard()
    if bad:
        return bad
    qq = _qq_arg()
    if not qq:
        return jsonify({"ok": False, "error": "没登录"}), 401
    tag = str(request.args.get("tag") or "").strip()
    with _JOBS_LOCK:
        row = _JOBS.get(tag)
        if row is None or row.get("qq") != qq:
            return jsonify({"ok": False, "error": "没有这个任务"}), 404
        job = row.get("job")
        out = {"tag": tag, "state": row["state"], "file": row["file"],
               "error": row["error"], "seed": row["seed"]}
    out["ahead"] = image_jobs.ahead_of(job) if job is not None else 0
    return jsonify({"ok": True, "job": out})


@bp.route("/api/friend/img")
def api_img():
    bad = _guard()
    if bad:
        return bad
    qq = _qq_arg()
    if not qq:
        return jsonify({"ok": False, "error": "没登录"}), 401
    name = str(request.args.get("f") or "").strip()
    if not _IMG_RE.match(name):
        return jsonify({"ok": False, "error": "文件名不对"}), 400
    path = os.path.join(_gallery_dir(qq), name)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "这张已经清了"}), 404
    mime = "image/webp" if name.lower().endswith(".webp") else "image/png"
    # 图是自己的，但别让中间层存——退出登录后不该还能从缓存里翻出来
    resp = send_file(path, mimetype=mime, conditional=False)
    resp.headers["Cache-Control"] = "private, max-age=600"
    return resp


@bp.route("/api/friend/history")
def api_history():
    bad = _guard()
    if bad:
        return bad
    qq = _qq_arg()
    if not qq:
        return jsonify({"ok": False, "error": "没登录"}), 401
    rows = [r for r in _reader(_HISTORY, 400) if r.get("qq") == qq]
    rows.sort(key=lambda r: r.get("ts", 0), reverse=True)
    out = []
    for r in rows[:60]:
        name = str(r.get("file") or "")
        alive = bool(name) and os.path.isfile(os.path.join(_gallery_dir(qq), name))
        out.append({"tag": r.get("tag"), "ok": bool(r.get("ok")),
                    "file": name if alive else "", "ts": r.get("ts"),
                    "loras": r.get("loras") or [],
                    "error": r.get("error") or ""})
    return jsonify({"ok": True, "rows": out})


@bp.route("/api/friend/presets")
def api_presets():
    bad = _guard()
    if bad:
        return bad
    qq = _qq_arg()
    if not qq:
        return jsonify({"ok": False, "error": "没登录"}), 401
    mine = lora_presets.for_qq(qq)
    rows = [{"name": k, "loras": v.get("loras") or [],
             "public": bool(v.get("public")), "ts": v.get("ts")}
            for k, v in mine.items() if isinstance(v, dict)]
    # 公开的排最前——它是唯一「别人也能用」的那个，值得占第一格
    rows.sort(key=lambda r: (not r["public"], -(r.get("ts") or 0)))
    return jsonify({"ok": True, "rows": rows,
                    "quota": lora_presets.quota(qq),
                    "prefix": lora_presets.PREFIX})


@bp.route("/api/friend/preset", methods=["POST"])
def api_preset_save():
    bad = _guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    if not friend_auth.QQ_RE.match(qq):
        return jsonify({"ok": False, "error": "没登录"}), 401
    slots, err = _slots_or_empty(data.get("loras"))
    if err:
        return jsonify({"ok": False, "error": err}), 400
    public = bool(data.get("public"))
    ok, msg = lora_presets.save(qq, data.get("name"), slots or [],
                                public=public, skill=SKILL)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    name = lora_presets.clean_name(data.get("name"))
    on = [s for s in (slots or []) if s.get("on")]
    log.info("群友页 %s 存预设「%s」（%s，%d 个 LoRA）", qq, name,
             "公开" if public else "私有", len(on))
    return jsonify({"ok": True, "name": name, "msg": msg,
                    "quota": lora_presets.quota(qq)})


@bp.route("/api/friend/preset/public", methods=["POST"])
def api_preset_public():
    """只切公开状态。单开一个口，因为网页上是个开关，不必把槽位重存一遍。

    每人只留一个公开：公开新的会自动把旧的降成私有（用户 2026-10-30 拍板的
    「自动顶替」），返回的 msg 里会点名是哪个被顶掉了。
    """
    bad = _guard()
    if bad:
        return bad
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    if not friend_auth.QQ_RE.match(qq):
        return jsonify({"ok": False, "error": "没登录"}), 401
    ok, msg = lora_presets.set_public(qq, data.get("name"),
                                      bool(data.get("public")))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 400
    log.info("群友页 %s 预设公开状态变更：%s", qq, msg)
    return jsonify({"ok": True, "msg": msg, "quota": lora_presets.quota(qq)})


@bp.route("/api/friend/preset", methods=["DELETE"])
def api_preset_del():
    bad = _guard()
    if bad:
        return bad
    qq = _qq_arg()
    if not qq:
        return jsonify({"ok": False, "error": "没登录"}), 401
    if not lora_presets.delete(qq, request.args.get("name")):
        return jsonify({"ok": False, "error": "没有这个预设"}), 404
    return jsonify({"ok": True, "quota": lora_presets.quota(qq)})
