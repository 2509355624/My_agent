# -*- coding: utf-8 -*-
"""「/跑图台」网页后端（2026-11-13，挂在 5174 主进程里）。

## 它做什么

QQ 私聊/群发 `/跑图台` → 机器人回一条带令牌的网页链接 → 用户点开，看到
**两张 Tab**：

- **官方渠道**（放最前）：`gen_api.channels()` 里那 51 个生图渠道（跟生图页
  同一份数据源），每个一张卡片（封面 + 中文名 + 简介）。点卡片 = 渠道已定死，
  只需填一句中文需求 → 后端走 `direct_gen._translate`（LLM 把中文需求转成
  prompt）+ `_enqueue` 出图，结果推回发出命令的那个私聊/群。
- **我的预设**：`runbench_api.list_names()` 里用户 `/存预设` 存的随心配配方，
  点卡片填一句提示词 → 用存的 recipe（models/loras/seed）出图。

## 和换装（outfit_api）的异同

同一套「令牌免登录」骨架——令牌本身就是凭证，同时携带「结果推回哪个会话」，
不登录、不记账号，分享给别人也能用，结果仍回原会话。

## 封面归属（2026-11-13 用户拍板，纠正前版「预设封面」误解）

- **官方渠道封面**：key = 渠道 id（如 `anima-2s`），图片管理页「存为封面」
  从图 rel 里读出渠道名存成 `runbench_covers/<渠道id>.png`。跑图台官方渠道
  卡片显示它。**不是**存成什么「可爱画风」这种预设名。
- **预设封面**：key = 预设名，`/存预设` 时自动抓大大怪刚出的图当封面
  （`direct_gen._save_preset_text` 里读 `anima_random.load_last()["image_rel"]`）。

## 为什么网页不需要轮询出图状态

出图结果**只推回 QQ**（`_translate`+`_enqueue` 内部 `image_jobs` 自动发图），
网页只需要「已提交 / 提交失败」这一句，不做缩略图轮询。

## 限流 / 防滥用

- 令牌免登录，但每令牌在途 ≤ 1、全局在途 ≤ 4、每 IP 提交 ≤ 8/小时。
- 令牌 6 小时过期 + HMAC 签名（签名域 runbench:，与换装/fr_sess 分开）。
"""

import json
import logging
import os
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app import friend_auth, qq_api, runbench_api
from app.config import state_path

log = logging.getLogger("runbench_web")

bp = Blueprint("runbench", __name__)

_PER_TOKEN_INFLIGHT = 1          # 每令牌在途 ≤ 1
_ALL_INFLIGHT = 4                # 全局在途 ≤ 4
_SUBMIT_PER_IP_HOUR = 8          # 每 IP 每小时 ≤ 8 张
_PROMPT_MAX = 10000              # 提示词长度上限（2026-11-13 用户要 1 万：
                                 # 有人会把整套 tag 串/分镜脚本直接贴进来，
                                 # 400 字连一段 danbooru 串都放不下）

# 封面缩略图浏览器缓存时长（秒）。封面很少变，immutable + 长 max-age 让浏览器
# 刷新不重下；改名覆盖时文件 mtime 变 → ETag 变 → 自动重新拉，不会吃旧图。
COVER_MAX_AGE = 86400            # 1 天

# 进程内任务表：tag -> {token, kind, name, prompt, state, error, ts}
# kind = "channel"（官方渠道）| "preset"（我的预设）
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_IP_SUBMITS = {}                 # ip -> [timestamps]（每小时滑窗）
_IP_LOCK = threading.Lock()


# ─── 小工具 ──────────────────────────────────────────────

def _check_token():
    """从请求里取令牌并验签。→ (k, (target, target_id)) 成功；(None, resp) 失败。"""
    k = (request.args.get("k") or request.form.get("k")
         or (request.get_json(silent=True) or {}).get("k") or "").strip()
    parsed = friend_auth.parse_runbench_token(k)
    if not parsed:
        return None, (jsonify({"ok": False,
                               "error": "链接无效或已过期，回 QQ 再发一次 /跑图台"}), 401)
    return k, parsed


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


def _channel_rows():
    """官方渠道列表 → 网页卡片 JSON 行（id + 中文名 + 简介 + 封面）。"""
    from app import gen_api
    rows = []
    for c in gen_api.channels():
        cid = c.get("id") or ""
        if not cid:
            continue
        rows.append({
            "id": cid,
            "label": c.get("label") or cid,
            "summary": c.get("summary") or "",
            "cover": "/api/runbench/cover/%s" % cid,
            "has_cover": runbench_api.has_cover(cid),
            "cloud": bool(c.get("cloud")),
        })
    return rows


def _preset_rows():
    """预设列表 → 网页卡片 JSON 行（名字 + 封面 + 配方摘要）。"""
    rows = []
    for name in runbench_api.list_names():
        rec = runbench_api.get(name) or {}
        rows.append({
            "name": name,
            "cover": "/api/runbench/cover/%s" % name,
            "has_cover": runbench_api.has_cover(name),
            "n_model": len(rec.get("models") or []),
            "n_lora": len(rec.get("loras") or []),
            "seed": rec.get("seed"),
        })
    return rows


# ─── 路由 ────────────────────────────────────────────────

@bp.route("/api/runbench/check", methods=["GET"])
def api_check():
    """页面打开先探令牌（免登录），回官方渠道 + 预设列表给前端渲染卡片。"""
    k, parsed = _check_token()
    if k is None:
        return parsed
    target, target_id = parsed
    return jsonify({"ok": True, "target": target,
                    "channels": _channel_rows(),
                    "presets": _preset_rows(),
                    "prompt_max": _PROMPT_MAX})


@bp.route("/api/runbench/list", methods=["GET"])
def api_list():
    """免令牌版列表（官方渠道 + 预设）。跑图台页面用，不涉及出图。"""
    return jsonify({"ok": True,
                    "channels": _channel_rows(),
                    "presets": _preset_rows()})


@bp.route("/api/runbench/gen", methods=["POST"])
def api_gen():
    """点卡片出图：验令牌 → 官方渠道走 _translate+_enqueue / 预设走 recipe →
    后台推回 QQ。body: {k, kind: "channel"|"preset", name, prompt, image}。

    image 可选：上传图的 data URL（前端已压成缩略图，长边 ~960px）。传了图 =
    先反推提示词（_recall_tags）再转译；不传图 = 纯文生图。**不做图生图垫图**
    （用户拍板：上传图是「看图说话」，反推出提示词，再文生图）。"""
    k, parsed = _check_token()
    if k is None:
        return parsed
    target, target_id = parsed
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind") or "").strip()
    name = str(body.get("name") or "").strip()
    prompt = str(body.get("prompt") or "").strip()
    image = str(body.get("image") or "").strip()
    if kind not in ("channel", "preset"):
        return jsonify({"ok": False, "error": "出图类型不对"}), 400
    if not name:
        return jsonify({"ok": False, "error": "没选卡片"}), 400
    if not prompt and not image:
        return jsonify({"ok": False, "error": "写一句想画的内容，或传一张图"}), 400
    if len(prompt) > _PROMPT_MAX:
        return jsonify({"ok": False,
                        "error": "提示词太长（%d 字，最多 %d）"
                                 % (len(prompt), _PROMPT_MAX)}), 400
    # 上传图防呆：必须是 data URL（前端压好的缩略图），且大小有上限（原图会
    # 被前端 Canvas 压到几十 KB，这里挡误传/恶意大包）。
    if image and not image.startswith("data:image/"):
        return jsonify({"ok": False, "error": "图片格式不对，重新传"}), 400
    if image and len(image) > (6 << 20):           # 6MB，缩略图远用不到
        return jsonify({"ok": False, "error": "图片太大了（传前会压缩成缩略图）"}), 400

    if kind == "preset":
        pname, err = runbench_api.resolve(name)
        if err:
            return jsonify({"ok": False, "error": err}), 404
        name = pname
    else:
        # 官方渠道：校验这个 id 在渠道表里（防手改 id 打未知渠道）。
        if not any(c["id"] == name for c in _channel_rows()):
            return jsonify({"ok": False,
                            "error": "没有「%s」这个渠道" % name}), 404

    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    if not _ip_ok(ip):
        return jsonify({"ok": False, "error": "这一小时发太多次了，歇会儿"}), 429
    if _token_inflight(k) >= _PER_TOKEN_INFLIGHT:
        return jsonify({"ok": False, "error": "上一张还在跑，等它出来再点下一张"}), 429
    if _inflight() >= _ALL_INFLIGHT:
        return jsonify({"ok": False, "error": "前面排队的人有点多，稍等再试"}), 429

    tag = "RB-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    with _JOBS_LOCK:
        _JOBS[tag] = {"token": k, "kind": kind, "name": name, "prompt": prompt,
                      "image": image, "state": "queued", "error": "", "ts": time.time()}
    threading.Thread(target=_run, args=(tag, k, parsed, kind, name, prompt, image),
                     daemon=True).start()
    return jsonify({"ok": True, "tag": tag, "kind": kind, "name": name})


@bp.route("/api/runbench/status", methods=["GET"])
def api_status():
    """网页轮询出图状态（提交是否成功 / 出错）。"""
    k, parsed = _check_token()
    if k is None:
        return parsed
    tag = (request.args.get("tag") or "").strip()
    with _JOBS_LOCK:
        row = _JOBS.get(tag)
    if not row or row.get("token") != k:
        return jsonify({"ok": False, "error": "找不到这次任务"}), 404
    return jsonify({"ok": True, "state": row.get("state"),
                    "error": row.get("error") or ""})


@bp.route("/api/runbench/cover/<name>", methods=["GET"])
def api_cover(name):
    """封面缩略图。跑图台卡片 `<img>` 直接用，免令牌（封面本身无敏感信息）。

    懒生成：第一次请求时把原图 PNG（8~16MB）压成长边 480px 的 JPEG 缩略图
    存盘，之后直接发缩略图（~30KB）。缩略图带 `immutable` 长期缓存 + ETag，
    浏览器刷新不再重下，改名覆盖（mtime 变）时 ETag 自动失效会重拉。
    """
    thumb = runbench_api.ensure_thumb(name)
    if not thumb:
        return "", 404
    resp = send_file(thumb, mimetype="image/jpeg", conditional=True,
                     max_age=COVER_MAX_AGE)
    resp.headers["Cache-Control"] = "public, max-age=%d, immutable" % COVER_MAX_AGE
    return resp


# ─── 后台出图 ────────────────────────────────────────────

def _run(tag, k, parsed, kind, name, prompt, image=""):
    """绑定线程上下文 → 按 kind 出图 → 结果自动推回 QQ。"""
    target, target_id = parsed
    try:
        with _JOBS_LOCK:
            _JOBS[tag]["state"] = "running"
        # 把「结果推回哪个会话」绑到线程本地：_translate/_enqueue/_generate_image
        # 内部的 qq_api.current_context() 靠它定位目标，image_jobs 发图也走它。
        qq_api.bind_context(
            "%s_%s" % (target, target_id), target, target_id, user_text=prompt)
        if kind == "preset":
            text = _run_preset(prompt, name)
        else:
            text = _run_channel(prompt, name, image)
        # text 是回执：失败都以「错误」开头（错误: / 错误：），成功（入队成功、
        # 回执、甚至「重复图已在队列」）都不带这个前缀。用「不以错误开头」当
        # 成功判据，比「以 RECEIPT_SENT_MARK 开头」更稳——回执没直发成功时
        # 返回的是「已经排上队了…」，那也是成功。
        if text.startswith("错误"):
            with _JOBS_LOCK:
                _JOBS[tag]["state"] = "error"
                _JOBS[tag]["error"] = text
            _notify_fail(target, target_id, text)
        else:
            with _JOBS_LOCK:
                _JOBS[tag]["state"] = "done"
            log.info("跑图台 %s：%s 用「%s」出图已提交",
                     target, target_id, name)
    except Exception as e:
        err = str(e) or e.__class__.__name__
        log.warning("跑图台任务 %s 失败：%s", tag, err)
        with _JOBS_LOCK:
            _JOBS[tag]["state"] = "error"
            _JOBS[tag]["error"] = err
        _notify_fail(target, target_id, "跑图没跑成：" + err)
    finally:
        try:
            qq_api.clear_context()
        except Exception:
            pass


def _run_preset(prompt, preset):
    """预设出图：复用 generate_image 的 _anima_random 分支读 recipe。"""
    from app.tools.normal import generate_image
    result = generate_image._generate_image(
        prompt, skill="_anima_random", anima_preset=preset, _skip_confirm=True)
    return str(result or "").strip()


def _run_channel(prompt, skill, image=""):
    """官方渠道出图：走 direct_gen 的「中文需求 → prompt → 入队」闭环。

    和 QQ 里发「渠道名 + 需求」完全等价：`_translate` 用 LLM 把中文需求转成
    英文 tag，`_enqueue` 入队出图、结果推回 QQ。渠道已由卡片定死（skill 非空
    = 点名渠道最高优先级）。

    image 非空（用户上传了图）= 先 `_recall_tags` 反推出英文提示词，跟用户写的
    需求拼一起再转译。这是「看图说话」，**不是图生图垫图**（用户拍板）。
    """
    from app import direct_gen as dg
    if image:
        try:
            # 反推：图 → 英文混合式提示词（标签打底 + 短语补关系）。失败不
            # 阻断——图反推不出还能靠用户写的需求文生图，别让一张读不出的图
            # 把整单掐死。
            tags = dg._recall_tags([image], numbered=False)
        except Exception:
            log.warning("跑图台反推失败，退回纯文生图", exc_info=True)
            tags = ""
        # 反推出的英文提示词是「画面事实」，用户需求是「想要的效果」，两句都
        # 交给 _translate 一起转译（引用图 + 需求 在 QQ 里也是这么合并的）。
        text = (prompt or "")
        if tags:
            text = (tags + ("。" if not tags.endswith((".", "，", ",")) else "")
                    + (("\n用户想要：" + prompt) if text else ""))
    else:
        text = prompt
    trans = dg._translate(text, history=[], skill=skill)
    if not trans:
        return "错误: 提示词转译没成功，再试一次。"
    if "reply" in trans:
        # 模型判断这轮不用画（比如问渠道本身的事）→ 把回复推回 QQ。
        return str(trans["reply"] or "").strip()
    if "skill" in trans:
        # 出图任务：用转译出的 skill/prompt 入队。skill 可能被模型改成别的
        # 渠道，但点名渠道是最高优先级，_translate 里已把话说死，这里照单入队。
        return dg._enqueue(trans["skill"], trans["prompt"], text)
    return "错误: 提示词转译结果看不懂，再试一次。"


def _notify_fail(target, target_id, msg):
    """出图被拒/报错时，把原因推回 QQ（不让用户干等一张出不来的图）。"""
    try:
        txt = "%s\n回 QQ 再点一次卡片重试。" % msg
        if target == "private":
            qq_api.send_private(int(target_id), txt)
        else:
            qq_api.send_group(int(target_id), txt)
    except Exception:
        log.warning("跑图台失败通知也没发出去", exc_info=True)
