# -*- coding: utf-8 -*-
"""「/换装」网页后端（2026-11-11，挂在 5174 主进程里）。

## 它做什么

QQ 私聊/群发 `/换装` → 机器人回一条带令牌的网页链接 → 用户点开、传两张图、
选「换衣服 / 换姿势」→ 本模块把两张图送进 ComfyUI、用 qwen-image-2.1 的
**多图参考**把图 A 的衣服/姿势迁移到图 B → 出图落本机，网页看缩略图，同时
把原图推回发出命令的那个私聊/群。

## 为什么独立成模块，不塞进 comic_api

换装和漫画台/写真台没有共享状态：不进 `comic_quota` 额度、不落 `comics/`
目录、不走 `comic_delivery` 状态机、不按账号归属。它只有「令牌 → 来源」这
一条线，独立一份最干净；将来要下线也是拔一个 Blueprint 的事。

## 双图怎么进 ComfyUI（和单图 i2i 不一样）

`image_jobs` 的 `i2i_src` 延迟上传只支持**一张**源图（一个占位名
`__I2I_SRC__`）。换装要两张，所以不走那条路：入队前用
`comfy_src.upload(wait=True)` 把两张图各自上传、拿到 ComfyUI input 目录里的
真实文件名，直接替换工作流里的 `__OUTFIT_TARGET__` / `__OUTFIT_SOURCE__`。
`wait=True` 正是给「调用方自己要文件名、紧接着拼工作流」用的（见其注释）。

## 图序（2026-11-11 站主与 Qwen 官方约定对齐）

用户直觉：图1 = 衣服/姿势**来源**，图2 = **被穿的人**（"把图1的衣服换到图2"）。
Qwen 约定：`images.image_1` = 被编辑的**主图/要保留的人**，`images.image_2` =
参考图。`latent_image` 也从 image_1 出（尺寸/构图由主图定）。

→ 网页两个框按用户直觉标（左来源、右被穿的人）；后端**对调**再喂节点：
被穿的人（用户图2）→ `__OUTFIT_TARGET__` → node13 → `image_1`（主图）；
来源（用户图1）→ `__OUTFIT_SOURCE__` → node14 → `image_2`（参考）。
代码里只认语义名，不认"图1图2"，避免绕晕。

## 限流 / 防滥用

- 令牌免登录（QQ 可信通道发的链接），但**每令牌在途 ≤ 1**（换装是单张低频，
  连点只占一个位）、**全局在途 ≤ 4**（不跟 QQ/漫画抢整条独木桥）、
  每 IP 提交 ≤ 8/小时。
- 令牌 6 小时过期 + HMAC 签名，篡改/伪造过不了。
- 上传图先经浏览器 Canvas 压到长边 960px（几十 KB）再过隧道，原图不出本机。

## 图存哪

`state/outfit/<令牌hash[:12]>/`：两张原图 `a.png` `b.png` + 成果 `result.png`
+ 状态 `job.json`。成果同时推回 QQ，网页只看服务端磁盘上的缩略图（小图过隧道）。
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid

from flask import Blueprint, jsonify, request, send_file

from app import comfy_src, friend_auth, image_jobs, qq_api
from app.config import state_path
from app.skills import load_workflow

log = logging.getLogger("outfit_api")

bp = Blueprint("outfit", __name__)

# 双图工作流骨架（_ 前缀 = 内部专用，不出现在普通渠道列表里）
_SKILL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "_qwen_outfit")
_WORKFLOW = os.path.join(_SKILL_DIR, "workflow.json")

_ROOT = state_path("outfit")
_PER_TOKEN_INFLIGHT = 1          # 每令牌在途 ≤ 1（换装单张低频）
_ALL_INFLIGHT = 4                # 全局在途 ≤ 4（不抢 QQ/漫画的独木桥）
_UPLOAD_MAX = 12 << 20           # 单张上传上限 12MB（前端已压到几十 KB，这是防呆）
_SUBMIT_PER_IP_HOUR = 8          # 每 IP 每小时最多提交 8 张

# 固定提示词（英文祈使句，主图 + 参考 + 保留项三段式；零 LLM）。
# 依据：阿里云 Qwen-Image-Edit 官方例 + Qwen-Image-2.1 提示词手册 +
# 社区实测 skill——Qwen 图编辑吃「一句祈使指令 + 显式锁定保留项」，
# 不是场景描述。换装/换姿势是确定性操作，没有要模型临场判断的部分。
PROMPTS = {
    "outfit": ("Keep the person in the first image unchanged — face, identity, "
               "pose, body and background. Replace only the clothing with the "
               "outfit from the second image, matching its design, color and "
               "texture faithfully."),
    "pose": ("Keep the person in the first image unchanged — face, identity, "
             "outfit and background. Replace only the pose with the pose of the "
             "person in the second image, matching arm and hand positions and "
             "body orientation faithfully."),
}
MODES = tuple(PROMPTS)           # ("outfit", "pose")

# 进程内任务表：tag -> {token, mode, state, file, error, ts}
_JOBS = {}
_JOBS_LOCK = threading.Lock()
# 全局在途计数（用 _JOBS 现算即可，不必另存）
_IP_SUBMITS = {}                 # ip -> [timestamps]（每小时滑窗）
_IP_LOCK = threading.Lock()


# ─── 小工具 ──────────────────────────────────────────────

def _tdir(k):
    """令牌 → 该批次的落盘目录（令牌只含 hex/点/下划线，取 hash 更稳）。"""
    h = hashlib.sha256(str(k).encode("utf-8")).hexdigest()[:12]
    return os.path.join(_ROOT, h)


def _read_job(tag):
    with _JOBS_LOCK:
        row = _JOBS.get(tag)
        return dict(row) if row else None


def _write_job(tag, **kw):
    with _JOBS_LOCK:
        row = _JOBS.setdefault(tag, {})
        row.update(kw)
        return dict(row)


def _inflight():
    with _JOBS_LOCK:
        return sum(1 for r in _JOBS.values()
                   if r.get("state") in ("queued", "running"))


def _token_inflight(k):
    with _JOBS_LOCK:
        return sum(1 for r in _JOBS.values()
                   if r.get("token") == k
                   and r.get("state") in ("queued", "running"))


def _ip_ok(ip):
    now = time.time()
    with _IP_LOCK:
        hits = [t for t in _IP_SUBMITS.get(ip, []) if now - t < 3600]
        if len(hits) >= _SUBMIT_PER_IP_HOUR:
            _IP_SUBMITS[ip] = hits
            return False
        hits.append(now)
        _IP_SUBMITS[ip] = hits
        if len(_IP_SUBMITS) > 5000:
            for key in [k for k, v in _IP_SUBMITS.items()
                        if not v or now - v[-1] > 3600]:
                _IP_SUBMITS.pop(key, None)
    return True


def _check_token():
    """从请求里取令牌并验签。→ (k, target, target_id) 或 (None, None, resp)。"""
    k = (request.args.get("k") or request.form.get("k")
         or (request.get_json(silent=True) or {}).get("k") or "").strip()
    parsed = friend_auth.parse_outfit_token(k)
    if not parsed:
        return None, None, (jsonify({"ok": False,
                                     "error": "链接无效或已过期，回 QQ 再发一次 /换装"}), 401)
    return k, parsed, None


def _save_png(fs, dst):
    """上传图统一转 PNG 落盘 + 正 EXIF（复用漫画台的口径，抄一份避免跨模块耦合）。"""
    from PIL import Image, ImageOps
    import io
    im = Image.open(io.BytesIO(fs.read()))
    try:
        im.load()
        im = ImageOps.exif_transpose(im) or im
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        im.save(dst, "PNG")
    finally:
        im.close()


def _build_workflow(target_name, source_name, mode):
    """读骨架 → 替两张图 + 提示词 + 种子 → 返回可提交的 workflow dict。"""
    wf = load_workflow(_WORKFLOW)
    if not wf:
        raise RuntimeError("换装工作流读不出来（服务端缺 skills/_qwen_outfit/workflow.json）")
    s = json.dumps(wf)
    s = s.replace('"__OUTFIT_TARGET__"', json.dumps(target_name))
    s = s.replace('"__OUTFIT_SOURCE__"', json.dumps(source_name))
    s = s.replace("__MULTI_PROMPTS__",
                  PROMPTS[mode].replace("\\", "\\\\").replace('"', '\\"'))
    # 单一 seed：带引号/裸占位符都替成同一个数（骨架里 load_workflow 已把裸
    # __SEED__ 补成带引号，这里第一次就替掉，第二次是兜底；两者同值不会分叉）。
    seed = str(uuid.uuid4().int % (2 ** 31))
    s = s.replace('"__SEED__"', seed).replace("__SEED__", seed)
    return json.loads(s)


# ─── 路由 ────────────────────────────────────────────────

@bp.route("/api/outfit/check", methods=["GET"])
def api_check():
    """页面打开先探一下令牌（免登录），顺便回固定配置给前端。"""
    k, parsed, err = _check_token()
    if err:
        return err
    target, target_id = parsed
    return jsonify({"ok": True, "target": target,
                    "modes": list(MODES), "max_upload": _UPLOAD_MAX})


@bp.route("/api/outfit/upload", methods=["POST"])
def api_upload():
    """收两张图（前端已压到长边 960px），落盘到 state/outfit/<hash>/。"""
    k, parsed, err = _check_token()
    if err:
        return err
    a, b = request.files.get("a"), request.files.get("b")
    if not a or not b:
        return jsonify({"ok": False, "error": "要传两张图：左边来源、右边被穿的人"}), 400
    # 大小防呆：前端已压到几十 KB，这里挡住误传原图/恶意大包把内存顶爆。
    for fs in (a, b):
        try:
            fs.stream.seek(0, os.SEEK_END)
            size = fs.stream.tell()
            fs.stream.seek(0)
        except (OSError, ValueError):
            size = 0
        if size > _UPLOAD_MAX or size == 0:
            return jsonify({"ok": False,
                            "error": "图片太大或为空（单张上限 12MB，传前会压缩）"}), 400
    d = _tdir(k)
    try:
        _save_png(a, os.path.join(d, "a.png"))
        _save_png(b, os.path.join(d, "b.png"))
    except Exception as e:
        log.warning("换装上传解析失败：%s", e)
        return jsonify({"ok": False, "error": "这俩文件不像图片，换张图试试"}), 400
    return jsonify({"ok": True, "uploaded": True})


@bp.route("/api/outfit/gen", methods=["POST"])
def api_gen():
    """把两张图送进 ComfyUI 排队；出完在后台推回 QQ。"""
    k, parsed, err = _check_token()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    mode = str(body.get("mode") or "outfit").strip().lower()
    if mode not in MODES:
        return jsonify({"ok": False, "error": "只能换衣服或换姿势"}), 400
    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    if not _ip_ok(ip):
        return jsonify({"ok": False, "error": "这一小时发太多次了，歇会儿"}), 429
    if _token_inflight(k) >= _PER_TOKEN_INFLIGHT:
        return jsonify({"ok": False, "error": "上一张还在跑，等它出来再换下一张"}), 429
    if _inflight() >= _ALL_INFLIGHT:
        return jsonify({"ok": False, "error": "前面排队的人有点多，稍等再试"}), 429

    d = _tdir(k)
    pa, pb = os.path.join(d, "a.png"), os.path.join(d, "b.png")
    if not (os.path.isfile(pa) and os.path.isfile(pb)):
        return jsonify({"ok": False, "error": "先上传两张图"}), 400
    tag = "OF-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
    _write_job(tag, token=k, mode=mode, state="queued", file="", error="",
               ts=time.time())
    threading.Thread(target=_run, args=(tag, k, parsed, mode, pa, pb),
                     daemon=True).start()
    return jsonify({"ok": True, "tag": tag})


@bp.route("/api/outfit/status", methods=["GET"])
def api_status():
    """网页轮询出图状态。"""
    k, parsed, err = _check_token()
    if err:
        return err
    tag = (request.args.get("tag") or "").strip()
    row = _read_job(tag)
    if not row or row.get("token") != k:
        return jsonify({"ok": False, "error": "找不到这次任务"}), 404
    return jsonify({"ok": True, "state": row.get("state"),
                    "file": row.get("file") or "", "error": row.get("error") or ""})


@bp.route("/api/outfit/img", methods=["GET"])
def api_img():
    """出图缩略图：网页只看这个（小图过隧道），原图推 QQ 走本机带宽。"""
    k, parsed, err = _check_token()
    if err:
        return err
    tag = (request.args.get("tag") or "").strip()
    row = _read_job(tag)
    if not row or row.get("token") != k or not row.get("file"):
        return "", 404
    path = row["file"]
    if not os.path.isfile(path):
        return "", 404
    # 浏览器端缩略：PIL 现压一张 WEBP 内存里发，原图不出本机、不落额外文件
    import io
    from PIL import Image
    im = Image.open(path)
    im.thumbnail((960, 960))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=82)
    buf.seek(0)
    return send_file(buf, mimetype="image/webp")


# ─── 后台出图 ────────────────────────────────────────────

def _fetch_result(rel):
    """ComfyUI 的一张图 → 字节。先读盘，读不到走 /view（与群友页 _fetch 同口径）。

    不猜「最新一张」：名字来自 history 的 output_images，取不到就如实失败。
    """
    from app import comfy_out
    import requests
    p = comfy_out.local_output_file(rel)
    if p:
        try:
            with open(p, "rb") as f:
                return f.read()
        except OSError:
            log.warning("换装读盘取图失败 %s", p, exc_info=True)
    parts = str(rel or "").replace("\\", "/").strip("/").split("/")
    if not parts or not parts[-1]:
        return None
    params = {"filename": parts[-1]}
    if len(parts) > 1:
        params["subfolder"] = "/".join(parts[:-1])
    try:
        from app.config import COMFYUI_URL
        r = requests.get(COMFYUI_URL.rstrip("/") + "/view", params=params,
                         timeout=120)
    except requests.RequestException:
        log.warning("换装 /view 取图失败 %s", rel, exc_info=True)
        return None
    return r.content if (r.status_code == 200 and r.content) else None


def _run(tag, k, parsed, mode, path_a, path_b):
    """上传两张图 → 组工作流 → 入队 → 等结果 → 推 QQ → 落状态。全程带异常兜底。"""
    target, target_id = parsed
    try:
        _write_job(tag, state="running")
        # 两张图各自上传进 ComfyUI input（wait=True：等重启窗口过去再传）
        with open(path_a, "rb") as f:
            src = comfy_src.upload(f.read(), suffix=".png", wait=True)
        with open(path_b, "rb") as f:
            tgt = comfy_src.upload(f.read(), suffix=".png", wait=True)
        # 对调：被穿的人(图B) → image_1 主图；来源(图A) → image_2 参考
        wf = _build_workflow(tgt, src, mode)
        # target/target_id 传真实来源：队列记账与 per_session 限流按会话算。
        # quiet=True 关掉 image_jobs 自己的自动发图——推 QQ 由 _deliver 统一做。
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill="qwen_image_v1",
            prompt=PROMPTS[mode], seed=0,
            out_dir="换装", quiet=True)
        if job is None:
            raise RuntimeError(reason or "队列拒收")
        entry = job.wait()
        names = image_jobs.output_images(entry, job=job)
        if not names:
            raise RuntimeError("跑完了但没拿到图")
        # 结果取字节：先读盘（最快、不怕 ComfyUI 重启），读不到走 /view。
        # 与群友页 _fetch 同口径；不自己拼 COMFYUI_OUTPUT_DIR（配了空串会错）。
        data = _fetch_result(names[0])
        if not data:
            raise RuntimeError("图取不回来（ComfyUI 没响应）")
        # output 目录会被定期清，复制一份到 state/outfit/<hash>/ 保底。
        d = _tdir(k)
        out = os.path.join(d, "result.png")
        with open(out, "wb") as fw:
            fw.write(data)
        _write_job(tag, state="done", file=out)
        # 推回原会话：私聊发文件、群发图片消息段
        _deliver(target, target_id, out)
    except Exception as e:
        err = str(e) or e.__class__.__name__
        log.warning("换装任务 %s 失败：%s", tag, err)
        _write_job(tag, state="error", error=err)
        try:
            txt = "换装没跑成：%s\n回 QQ 再发一次 /换装 重试。" % err
            if target == "private":
                qq_api.send_private(int(target_id), txt)
            else:
                qq_api.send_group(int(target_id), txt)
        except Exception:
            log.warning("换装失败通知也没发出去", exc_info=True)


def _deliver(target, target_id, path):
    """出图推回原会话。私聊发原图文件，群发图片消息段（群里直接出图）。

    群这条**不用** `send_group(id, image_segment(...))`——send_group 收到单个
    dict 会当文本处理，图发不出来。走 `send_image`（漫画/moss 已在用，内部
    正确组 image 段、带非好友兜底）。私聊发原图文件、群发图片消息段，互斥，
    别两条都发（用户要的是「QQ 收到原图、网页只是压缩预览」）。
    """
    if target == "private":
        qq_api.send_private_file(int(target_id), path, name="换装结果.png")
    else:
        qq_api.send_image("group", target_id, path, caption="换装好了")
