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

## 限流 / 防滥用（2026-11-25 用户拍板：**全部删掉**）

原来这里有「每令牌在途 ≤ 1、全局在途 ≤ 4、每 IP 提交 ≤ 8/小时、高峰禁批量」
四道闸，全是这套令牌免登录骨架自带的。用户原话：「没有这种所谓的限额，直接
给我删掉」「高峰期也去掉」「ComfyUI 走的是串行队列，压根不会崩」。

现在的规则只剩一条，而且不在本模块：一个会话最多同时在途 5 张
（`image_jobs.MAX_INFLIGHT`，`jobs.enqueue()` 里判）。跑图台提交几张就是几张，
串行反推、串行入队，一张跑完才跑下一张，跟 QQ 那边完全一样。

- 令牌 6 小时过期 + HMAC 签名（签名域 runbench:，与换装/fr_sess 分开）。
"""

import json
import logging
import os
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app.gateways import friend_auth
from app.features.qq import api as qq_api
from app.features.bench import api as runbench_api
from app.config import state_path

log = logging.getLogger("runbench_web")

bp = Blueprint("runbench", __name__)

_PROMPT_MAX = 10000              # 提示词长度上限（2026-11-13 用户要 1 万：
                                 # 有人会把整套 tag 串/分镜脚本直接贴进来，
                                 # 400 字连一段 danbooru 串都放不下）
# ── 「连续多图」批量（2026-11-25 用户拍板）──────────────────────────
#
# 三种玩法，共用同一个批量接口：
#   - `same`（一图多风）：**一张参考图 + 最多 5 个渠道**，参考图只反推一次拿
#     英文 tags，5 个渠道共用（反推是整条链里最贵的一步，一张反推一次等于
#     白烧 4 次）。**没有需求框**——这个玩法本身就是「反推图片、跑 5 个不同
#     渠道」，用户不需要再写字。
#   - `each`（五图各画）：**最多 5 张图，每张自己选渠道**，没有提示词框——
#     走的就是「上传图 → 识图反推 → 出图」，需求为空。
#   - `text`（文生图五连）：**不要图，一句提示词必填**，5 个格子各选一个渠道，
#     同一句提示词在 5 家各画一张。没选渠道的格子直接跳过（选 3 个就跑 3 张）。
#
# 两条硬约束（都是用户原话）：
#   1. **一图多风里不允两个格子选同一个渠道**（「不允许选同一个渠道」）——
#      选重了整单拒，不是默默去重（去重会让人以为 5 张都在跑）。
#   2. **回执合并成一条**，且在**最后一张也入队之后**才发——每张发一条就是
#      刷屏；发早了会让人以为「后面几张也画好了」。
#
# 2026-11-25 用户拍板删掉的两条（原来在这里）：
#   - 高峰禁止批量（10–12 / 22–24）——「高峰期去掉吧，压根没用」。
#   - 每 IP 每小时 8 张——「没有这种所谓的限额」。
_BATCH_MAX = 5                   # 一次最多 5 张（UI 也是 5 格）
_PROMPT_MAX_BATCH = 20000        # 批量模式的提示词上限（用户：「我最长是允许
                                 # 2 万字的」）。单张那条 1 万的限制不动。
_IMAGE_MAX = 6 << 20             # 单张缩略图 data URL 上限（与单张一致）
_BATCH_MAX_BYTES = 40 << 20      # 整单请求体上限：5 张缩略图 + 2 万字提示词

# 封面缩略图浏览器缓存时长（秒）。封面很少变，immutable + 长 max-age 让浏览器
# 刷新不重下；改名覆盖时文件 mtime 变 → ETag 变 → 自动重新拉，不会吃旧图。
COVER_MAX_AGE = 86400            # 1 天
RUNBENCH_LIST_MAX_AGE = 300        # 跑图台渠道/预设列表 5 分钟，浏览器本地缓存


def _cached_json(payload, max_age=RUNBENCH_LIST_MAX_AGE):
    """跑图台列表走浏览器缓存：同一 URL 5 分钟内不再打服务端。

    渠道和预设不常变，但封面可能换；5 分钟足够短，换封面后刷新一次就能看到。
    `private` 是给带 k 令牌的 check 用的，避免被共享缓存串号。
    """
    resp = jsonify(payload)
    resp.headers["Cache-Control"] = "private, max-age=%d" % int(max_age)
    return resp

# 进程内任务表：tag -> {token, kind, name, prompt, state, error, ts, batch}
# kind = "channel"（官方渠道）| "preset"（我的预设）
# batch 非空 = 这一格属于某个「连续多图」批次，值是批次号。**只用于查进度和
# 合并回执**，不参与任何名额折算（2026-11-25 用户拍板：5 张就是 5 张，没有
# 「一批」这个概念）。
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_BATCHES = {}                    # 批次号 -> {state, total, queued, fail, ts}
_BATCH_LOCK = threading.Lock()


def _check_token():
    """从请求里取令牌并验签。→ (k, (target, target_id)) 成功；(None, resp) 失败。"""
    k = (request.args.get("k") or request.form.get("k")
         or (request.get_json(silent=True) or {}).get("k") or "").strip()
    parsed = friend_auth.parse_runbench_token(k)
    if not parsed:
        return None, (jsonify({"ok": False,
                               "error": "链接无效或已过期，回 QQ 再发一次 /跑图台"}), 401)
    return k, parsed


def _channel_rows():
    """官方渠道列表 → 网页卡片 JSON 行（id + 中文名 + 简介 + 封面）。"""
    from app.features.image import routes as gen_api
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
    return _cached_json({"ok": True, "target": target,
                         "channels": _channel_rows(),
                         "presets": _preset_rows(),
                         "prompt_max": _PROMPT_MAX})


@bp.route("/api/runbench/list", methods=["GET"])
def api_list():
    """免令牌版列表（官方渠道 + 预设）。跑图台页面用，不涉及出图。"""
    return _cached_json({"ok": True,
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

    # 2026-11-25 用户拍板：这里原来有三道闸（每 IP 每小时 8 张、每令牌在途 1、
    # 全局在途 4），全删。唯一的限制在 `image_jobs.enqueue()`：一个会话最多
    # 同时在途 5 张，那是全站统一的那一条，跑图台不另设。
    tag = "RB-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    with _JOBS_LOCK:
        _JOBS[tag] = {"token": k, "kind": kind, "name": name, "prompt": prompt,
                      "image": image, "state": "queued", "error": "", "ts": time.time()}
    threading.Thread(target=_run, args=(tag, k, parsed, kind, name, prompt, image),
                     daemon=True).start()
    return jsonify({"ok": True, "tag": tag, "kind": kind, "name": name})


def _parse_batch(body):
    """校验整单。→ ((mode, rows), err_msg, http_code)。

    rows 每项 {image, prompt, skill}。**整单先验完再动手**：半路才拒的话，
    前面几张已经入队了，用户看到的是「报了错，图却跑出来了」。
    """
    mode = str(body.get("mode") or "").strip()
    if mode not in ("same", "each", "text"):
        return None, "出图方式不对", 400
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return None, "没有要跑的图", 400
    prompt = str(body.get("prompt") or "").strip()
    if len(prompt) > _PROMPT_MAX_BATCH:
        return None, ("提示词太长（%d 字，最多 %d）"
                      % (len(prompt), _PROMPT_MAX_BATCH)), 400
    # 三种玩法各自的「信息源」：
    #   - same（一图多风）：**只要参考图**，没有需求框（用户 2026-11-25 拍板：
    #     「一图多风根本就没有必要加那个什么提示词输入栏，因为它本身就是反推
    #     图片，然后跑 5 种不同的渠道而已」）。前端若仍传了 prompt，这里直接
    #     丢掉，不让它掺进反推串里。
    #   - each（五图各画）：每格自己的图，也没有需求框。
    #   - text（文生图五连）：不要图，**一句提示词必填**。
    if mode == "same":
        prompt = ""

    known = {c["id"] for c in _channel_rows()}
    rows = []
    for i, it in enumerate(items[: _BATCH_MAX + 1], 1):
        it = it if isinstance(it, dict) else {}
        skill = str(it.get("skill") or "").strip()
        image = str(it.get("image") or "").strip()
        # 五图各画里「没传图的格子」、文生图五连里「没选渠道的格子」= 用户还没
        # 填，**跳过**而不是报错。用户 2026-11-25：「如果没有选渠道，那就默认
        # 跑 3 格、两格」——选了几个渠道就跑几张。
        if mode == "each" and not image:
            continue
        if mode == "text" and not skill:
            continue
        if not skill:
            return None, "第 %d 格没选渠道" % i, 400
        if skill not in known:
            return None, "没有「%s」这个渠道" % skill, 404
        # 重复渠道三种玩法一律放行（2026-11-26 用户拍板「抽卡」）：同一个提示词
        # 或同一张图连跑 5 次同一渠道、种子各不同，是明确需求，不是误选。
        # 选几个渠道就跑几张，撞渠道照单全跑、不去重。
        if image:
            if not image.startswith("data:image/"):
                return None, "第 %d 格的图片格式不对，重新传" % i, 400
            if len(image) > _IMAGE_MAX:
                return None, "第 %d 格的图片太大了" % i, 400
        # 「图也没有、话也没有」不用在这儿判：一图多风会在下面把参考图摊给
        # 所有格（只挂第一项、其余项天生是空串，在这儿判会把第 2 格误拒——
        # 用户看到的报错是「第 2 格没有图，也没有写需求」，可他明明传了图）；
        # 五图各画在上面已经把没图的格子 continue 掉了，文生图五连则不看图。
        # 真正「什么都没有」的整单，由下面按模式各自的兜底判住。
        rows.append({"image": image, "prompt": prompt, "skill": skill})

    if len(rows) > _BATCH_MAX:
        return None, "一次最多跑 %d 张" % _BATCH_MAX, 400
    if mode == "same":
        # 一图多风：一张参考图，多个渠道共用。有几个渠道就出几张。
        # 参考图**只发一份**（前端挂在第一项上，省请求体积），所以这里取
        # 「第一个带了图的项」，而不是死盯 rows[0]——第一格没选渠道时，图会在
        # 别的位置上。**没有需求**，出图全部靠这张图反推出来的提示词。
        img = ""
        for r in rows:
            if r["image"]:
                img = r["image"]
                break
        if not img:
            return None, "先传一张参考图", 400
        rows = [{"image": img, "prompt": "", "skill": r["skill"]}
                for r in rows]
    elif mode == "text":
        # 文生图五连：一句提示词，选中的几个渠道各画一张；没有图可反推，所以
        # 提示词必填——空着就什么都画不出来。
        if not prompt:
            return None, "写一句提示词再开跑", 400
        if not rows:
            return None, "至少选一个渠道", 400
        rows = [{"image": "", "prompt": prompt, "skill": r["skill"]}
                for r in rows]
    else:
        # 五图各画：一格一张图，**不要求每格都传**（传几张跑几张），但每张
        # 都得有图——这个模式下没有别的信息可画。
        rows = [r for r in rows if r["image"]]
        if not rows:
            return None, "先上传图片", 400
    if not rows:
        return None, "没有要跑的图", 400
    return (mode, rows), "", 0


@bp.route("/api/runbench/gen-many", methods=["POST"])
def api_gen_many():
    """「连续多图」批量下单：**一个接口收整单**，服务端串行逐张入队。

    用户拍板（不要前端 for 循环发 5 次）：「一个接口收整单、服务端批量排队」。

    流程：验令牌 → 整单校验（渠道/图片/重复渠道）→ 起一个后台线程 →
    线程里**串行**逐张识图、**识完一张立刻入队一张**（不等后面几张）→ 全部
    入队后**发一条合并回执**（每张发一条会刷屏）。

    2026-11-25 用户拍板：这里**没有任何配额/限流**（原来有「高峰禁批量」和
    「每 IP 每小时 8 张」两条，删了）。收到 5 张就是 5 张——串行反推、串行
    入队，ComfyUI 那边本来就是我们自己的串行队列，一张跑完才跑下一张。

    返回里带批次号，前端轮询 `/api/runbench/batch` 看进度打绿勾。绿勾是**进度
    提示、不是准入门槛**：本接口一返回，前端就可以退出页面，剩下的都在服务端跑。
    """
    k, parsed = _check_token()
    if k is None:
        return parsed
    target, target_id = parsed
    body = request.get_json(silent=True) or {}

    parsed_batch, err, code = _parse_batch(body)
    if err:
        return jsonify({"ok": False, "error": err}), code
    mode, rows = parsed_batch

    # 在途上限（2026-11-27 用户拍板）：一个会话同时在途仍受全站那道
    # MAX_INFLIGHT=5 管——11-25 删的是跑图台**自己**那几条闸（每令牌在途、
    # 全局在途、每 IP 每小时），不是这道全站闸。这里只做**前置人话提示**：
    # 提交那一刻按同一口径算还剩几个位，不够就整批退回并说清楚，免得用户
    # 以为排上了、后面几张却被 enqueue 静默拒掉。
    # 2026-11-27 现场：QQ 2141733573 一批五连被入两遍，日志 10 条「直达入队」
    # 却只扣了 5 张额度——重复的那遍全被 in-flight 闸拒了，回执还报成功。
    # 真正准入仍在 enqueue，二者口径同源，不是两套闸。
    from app.features.image import jobs as image_jobs
    limit, _label, _window = image_jobs.inflight_limit()
    cur = image_jobs.inflight_count(target, target_id)
    if cur + len(rows) > limit:
        return jsonify({"ok": False, "busy": True,
                        "error": ("现在有 %d 张在画或排队，同时最多 %d 张；"
                                  "这批 %d 张先不收，等前面出完再跑。"
                                  % (cur, limit, len(rows)))}), 429

    bid = "B-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
    with _BATCH_LOCK:
        _BATCHES[bid] = {"token": k, "state": "queued", "total": len(rows),
                         "queued": 0, "error": "", "ts": time.time(),
                         "mode": mode, "tags": []}
    threading.Thread(target=_run_batch,
                     args=(bid, k, parsed, mode, rows),
                     daemon=True).start()
    return jsonify({"ok": True, "bid": bid, "count": len(rows), "mode": mode})


@bp.route("/api/runbench/batch", methods=["GET"])
def api_batch_status():
    """前端轮询批量进度。`queued` = 已入队的张数（前端据此逐格打绿勾）。

    只报「推进了几张 / 有没有失败」，不报每张排到第几位——那个数一直在动，
    报出来只会让人反复刷新。
    """
    k, parsed = _check_token()
    if k is None:
        return parsed
    bid = (request.args.get("bid") or "").strip()
    with _BATCH_LOCK:
        row = dict(_BATCHES.get(bid) or {})
    if not row or row.get("token") != k:
        return jsonify({"ok": False, "error": "找不到这一批"}), 404
    return jsonify({"ok": True, "state": row.get("state"),
                    "total": row.get("total", 0),
                    "queued": row.get("queued", 0),
                    "error": row.get("error") or "",
                    "tags": row.get("tags") or []})


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


def _batch_add_job(bid, tag, k, item):
    """把批量里的一张登记进 `_JOBS`（带上批次号，计数时整组算一个）。"""
    with _JOBS_LOCK:
        _JOBS[tag] = {"token": k, "kind": "channel", "name": item["skill"],
                      "prompt": item["prompt"], "image": item["image"],
                      "state": "queued", "error": "", "ts": time.time(),
                      "batch": bid}


def _batch_advance(bid, tag="", err=""):
    """记一张入队成功（打绿勾）或记一条失败原因。"""
    with _BATCH_LOCK:
        row = _BATCHES.get(bid)
        if not row:
            return
        if tag:
            row.setdefault("tags", []).append(tag)
            row["queued"] = len(row["tags"])
        if err:
            fails = row.setdefault("fails", [])
            fails.append(err)


def _run_batch(bid, k, parsed, mode, rows):
    """串行逐张识图 → 识完一张**立刻**入队一张 → 全部入队后发**一条**回执。

    为什么串行、且不能并发识图：识图走的是同一个上游模型，并发 5 路除了把
    配额一下打满没有别的收益；而「识完一张立刻入队」是用户明确要的——第一张
    早排队一分钟，就早出一分钟。

    为什么批量期间要把单张回执吞掉：`_generate_image` 每入队一张就会直发一条
    回执（「任务已提交，前面还有 N 张」），5 张就是 5 条，用户原话「不要每张
    都发，不然会刷屏」。这里在进程内把直发通道临时调成「只存不发」，本批结束
    再恢复，最后自己拼一条合并回执发出去。**只影响本批**：别的会话、别的单张
    提交照旧直发（锁只在这一个线程的识别窗口内持有，见下面的 try/finally）。
    """
    target, target_id = parsed
    sent, fails = 0, []
    queued_skills = []          # 真正入队的渠道（回执列它，不列用户点的——作者点错/查重会不一致）
    swallow = _SwallowReceipts()
    try:
        tags = ""
        if mode == "same" and rows[0]["image"]:
            # 一图多风：参考图**只反推一次**，下游 5 个渠道共用这段英文 tags。
            tags = _recall_once(rows[0]["image"])
        qq_api.bind_context(
            "%s_%s" % (target, target_id), target, target_id,
            user_text=rows[0]["prompt"])
        with swallow:
            for item in rows:
                text = ""
                try:
                    per_tags = tags
                    if mode == "each":
                        per_tags = _recall_once(item["image"])
                    text = _run_channel(item["prompt"], item["skill"],
                                        image=item["image"], tags=per_tags)
                except Exception as e:      # 单张炸了不掐整批
                    log.warning("跑图台批量 %s：%s 那张失败", bid, item["skill"],
                                exc_info=True)
                    text = "错误：" + (str(e) or e.__class__.__name__)
                if text and text.startswith("错误"):
                    fails.append("%s：%s" % (item["skill"], text))
                    _batch_advance(bid, err="%s：%s" % (item["skill"], text))
                    continue
                tag = "RB-" + time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()
                _batch_add_job(bid, tag, k, item)
                _batch_advance(bid, tag=tag)
                # 入队已在上面同步完成（「识完一张立刻入队一张」）；这里只把
                # 状态收尾。原来还起一个 _run 线程、里面再调一次 _run_channel，
                # 等于同一张图入队两遍——2026-11-27 实测一批五连打出 10 条
                # 「直达入队」日志、额度只扣 5 张（后 5 张被 in-flight 闸拒）。
                with _JOBS_LOCK:
                    if tag in _JOBS:
                        _JOBS[tag]["state"] = "done"
                log.info("跑图台 %s：%s 用「%s」出图已提交",
                         target, target_id, item["skill"])
                sent += 1
                queued_skills.append(item["skill"])
    except Exception as e:
        err = str(e) or e.__class__.__name__
        log.warning("跑图台批量 %s 中断：%s", bid, err)
        fails.append(err)
    finally:
        try:
            qq_api.clear_context()
        except Exception:
            pass
        with _BATCH_LOCK:
            row = _BATCHES.get(bid)
            if row:
                row["state"] = "done" if sent else "error"
                if not sent:
                    row["error"] = "；".join(fails) or "一张都没排上"
        _send_batch_receipt(target, target_id, rows, sent, fails, bid,
                            queued_skills=queued_skills)


class _SwallowReceipts(object):
    """上下文管理器：批量期间把「回执直发」临时改成「只收集、不发送」。

    实现方式是替换 `image_jobs._send_text`（回执直发的**唯一出口**，见
    `generate_image._send_receipt`）。换来的是：不改 generate_image、不加新参数，
    也不会让别的地方跟着变——退出时原样装回去。

    `seam` = 被替换的函数本身，测试可以拿它断言「确实接管过」。
    """

    def __init__(self):
        self.seam = None

    def __enter__(self):
        from app.features.image import jobs as image_jobs
        self.seam = image_jobs._send_text
        image_jobs._send_text = lambda target, target_id, text: None
        return self

    def __exit__(self, *exc):
        from app.features.image import jobs as image_jobs
        if self.seam is not None:
            image_jobs._send_text = self.seam
        return False


def _recall_once(image):
    """一张图反推出英文 tags；失败返回 ""（不掐整单，退回纯文生图）。"""
    from app.features.image import direct_gen as dg
    if not image:
        return ""
    try:
        return (dg._recall_tags([image], numbered=False) or "").strip()
    except Exception:
        log.warning("跑图台批量：反推失败，退回纯文生图", exc_info=True)
        return ""


def _send_batch_receipt(target, target_id, rows, sent, fails, bid,
                        queued_skills=None):
    """一批跑完（全部入队）后发的**那一条**回执。

    用户原话：「回执发送回执的时候要等那五个全部都推完之后一起发个回执」
    「不要每张都发，不然会刷屏」。
    """
    from app.features.image import jobs as image_jobs
    lines = []
    if sent:
        lines.append("这 %d 张都排上队了，画好一张发一张。" % sent)
    if fails:
        lines.append("%d 张没排上：" % len(fails))
        lines.extend("· " + f for f in fails[:5])
    if sent:
        # 列**真正入队**的渠道，不列用户点的 rows：某格被拦/改判时两者会不一致，
        # 回执写用户点的会让对面以为「我选的渠道没了、有个渠道跑了两次」。
        skills = queued_skills if queued_skills else [r["skill"] for r in rows]
        names = "、".join(skills)
        lines.append("渠道：%s" % names)
    if not sent and not fails:
        lines.append("这批一张都没跑成，回跑图台再点一次。")
    body = "\n".join(lines)
    try:
        image_jobs._send_text(target, target_id, body)
    except Exception:
        log.warning("跑图台批量回执没发出去 %s", bid, exc_info=True)


def _run_preset(prompt, preset):
    """预设出图：复用 generate_image 的 _anima_random 分支读 recipe。"""
    from app.tools.normal import generate_image
    result = generate_image._generate_image(
        prompt, skill="_anima_random", anima_preset=preset, _skip_confirm=True)
    return str(result or "").strip()


def _run_channel(prompt, skill, image="", tags=None):
    """官方渠道出图：走 direct_gen 的「中文需求 → prompt → 入队」闭环。

    和 QQ 里发「渠道名 + 需求」完全等价：`_translate` 用 LLM 把中文需求转成
    英文 tag，`_enqueue` 入队出图、结果推回 QQ。渠道已由卡片定死（skill 非空
    = 点名渠道最高优先级）。

    image 非空（用户上传了图）= 先 `_recall_tags` 反推出英文提示词，跟用户写的
    需求拼一起再转译。这是「看图说话」，**不是图生图垫图**（用户拍板）。

    tags 非 None（批量「一图多风」）= 反推**已经做过了**，直接用这段英文，
    不再对同一个 image 再反推一遍（反推是整条链里最贵的一步，5 个渠道共用
    一张参考图时重复反推等于白烧 4 次）。
    """
    from app.features.image import direct_gen as dg
    if tags is not None:
        # 反推结果由调用方给（批量共用一张参考图时只推一次）。
        _tags = tags
    elif image:
        try:
            # 反推：图 → 英文混合式提示词（标签打底 + 短语补关系）。失败不
            # 阻断——图反推不出还能靠用户写的需求文生图，别让一张读不出的图
            # 把整单掐死。
            _tags = dg._recall_tags([image], numbered=False)
        except Exception:
            log.warning("跑图台反推失败，退回纯文生图", exc_info=True)
            _tags = ""
    else:
        _tags = ""
    # 反推出的英文提示词是「画面事实」，用户需求是「想要的效果」，两句都交给
    # _translate 一起转译（引用图 + 需求 在 QQ 里也是这么合并的）。
    text = (prompt or "")
    if _tags:
        _tags = str(_tags).strip()
        text = (_tags + ("。" if not _tags.endswith((".", "，", ",")) else "")
                + (("\n用户想要：" + prompt) if text else ""))
    trans = dg._translate(text, history=[], skill=skill)
    if not trans:
        return "错误: 提示词转译没成功，再试一次。"
    if "reply" in trans:
        # 模型判断这轮不用画（比如问渠道本身的事）。这在批量里是「这一格没
        # 排上」，必须以「错误」开头，否则 `_run_batch` 会拿它当成功打绿勾、
        # sent+=1，用户看到「5 张全排上」实际少一张（2026-11-26 查证）。
        return "错误: 这一格没排上——" + (str(trans["reply"] or "").strip()
                                     or "模型判断不用画")
    if "skill" in trans:
        # 出图任务：用转译出的 skill/prompt 入队。skill 可能被模型改成别的
        # 渠道，但点名渠道是最高优先级，_translate 里已把话说死，这里照单入队。
        # skip_dup=True：跑图台连排 5 张同渠道同提示词（种子各不同）是抽卡，
        # 不是模型的重复误提交——查重守卫只管 QQ 对话那条路（2026-11-26）。
        out = dg._enqueue(trans["skill"], trans["prompt"], text,
                          skip_dup=True)
        # enqueue 的拒收（在途满 / 队列满 / 重渠道满）返回的是**不带前缀**的
        # 人话，direct_gen._enqueue 只在「错误：」开头时才认失败——原样返回
        # 会被 _run_batch 当成入队成功、打假绿勾、回执报「都排上了」。
        # 这里翻成带前缀的一句（只截第一句，去掉尾巴那句喂模型的指令），
        # 让批量回执如实显示「X 张没排上：这个会话已经排着 5 张了」。
        # 入口 api_gen_many 已前置拦大额超额；这道兜住并发/单张路径的拒收。
        if (out and not out.startswith("错误")
                and "已经排着" in out):
            return "错误：" + out.split("。")[0].strip() + "。"
        return out
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
