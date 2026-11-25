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

from app.gateways import friend_auth
from app.features.qq import api as qq_api
from app.features.bench import api as runbench_api
from app.config import state_path

log = logging.getLogger("runbench_web")

bp = Blueprint("runbench", __name__)

_PER_TOKEN_INFLIGHT = 1          # 每令牌在途 ≤ 1
_ALL_INFLIGHT = 4                # 全局在途 ≤ 4
_SUBMIT_PER_IP_HOUR = 8          # 每 IP 每小时 ≤ 8 张
_PROMPT_MAX = 10000              # 提示词长度上限（2026-11-13 用户要 1 万：
                                 # 有人会把整套 tag 串/分镜脚本直接贴进来，
                                 # 400 字连一段 danbooru 串都放不下）

# ── 「连续多图」批量（2026-11-25 用户拍板）──────────────────────────
#
# 两种玩法，共用同一个批量接口：
#   - `same`（一图多风）：**一张参考图 + 一份需求 + 最多 5 个渠道**，参考图只
#     反推一次拿英文 tags，5 个渠道共用（反推是整条链里最贵的一步，一张反推
#     一次等于白烧 4 次）。
#   - `each`（五图各画）：**最多 5 张图，每张自己选渠道**，没有提示词框——
#     走的就是「上传图 → 识图反推 → 出图」，需求为空。
#
# 三条硬约束（都是用户原话）：
#   1. **同一批里不允两个格子选同一个渠道**（「不允许选同一个渠道」）——
#      选重了整单拒，不是默默去重（去重会让人以为 5 张都在跑）。
#   2. **高峰禁止批量**（10–12 / 22–24）：高峰每会话只让挂 1 张，一批 5 张
#      会把整条队列堵死 5 分钟，其它人一张都排不进。
#   3. **回执合并成一条**，且在**最后一张也入队之后**才发——每张发一条就是
#      刷屏；发早了会让人以为「后面几张也画好了」。
_BATCH_MAX = 5                   # 一批最多几张（UI 也是 5 格）
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
# batch 非空 = 这一格属于某个「连续多图」批次，值是批次号。计数时**同一批次
# 只算 1**（见 `_inflight` / `_token_inflight` 的 `_weigh`），否则一个人开一批
# 就把全局 4 个名额全占了。
_JOBS = {}
_JOBS_LOCK = threading.Lock()
_IP_SUBMITS = {}                 # ip -> [timestamps]（每小时滑窗）
_IP_LOCK = threading.Lock()
_BATCHES = {}                    # 批次号 -> {state, total, queued, fail, ts}
_BATCH_LOCK = threading.Lock()


def _weigh(rows):
    """把任务行折算成「占几个名额」：普通任务 1 个，同一批次整体按 1 个算。

    用户拍板：「一次占 5 个名额，其实就对」是指**占队列名额**（ComfyUI 那边
    该排 5 张就排 5 张）；而网页端的在途限制是**防一个人把入口堵死**的，一组
    五连是用户一次有意的点击，按 5 个单位算会直接把人挡在门外。
    """
    total = 0
    batches = set()
    for r in rows:
        b = r.get("batch")
        if b:
            batches.add(b)
        else:
            total += 1
    return total + len(batches)


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
        rows = [r for r in _JOBS.values()
                if r.get("state") in ("queued", "running")]
    return _weigh(rows)


def _token_inflight(k):
    with _JOBS_LOCK:
        rows = [r for r in _JOBS.values()
                if r.get("token") == k
                and r.get("state") in ("queued", "running")
                and not r.get("batch")]     # 批量整组算一个单位，在下面单独判
        batches = {r.get("batch") for r in _JOBS.values()
                   if r.get("token") == k
                   and r.get("state") in ("queued", "running")
                   and r.get("batch")}
    return len(rows) + len(batches)


def _ip_ok(ip, cost=1):
    """记一次提交并判额度。cost = 这一单算几张（批量按真实张数算）。"""
    now = time.time()
    with _IP_LOCK:
        hits = [t for t in _IP_SUBMITS.get(ip, []) if now - t < 3600]
        if len(hits) + cost > _SUBMIT_PER_IP_HOUR:
            _IP_SUBMITS[ip] = hits
            return False
        hits.extend([now] * max(1, int(cost)))
        _IP_SUBMITS[ip] = hits
        if len(_IP_SUBMITS) > 5000:
            for key in [k for k, v in _IP_SUBMITS.items()
                        if not v or now - v[-1] > 3600]:
                _IP_SUBMITS.pop(key, None)
    return True


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


def _batch_peak_blocked():
    """高峰档禁止批量。返回 (是否拦下, 给的提示话术)。

    判据现读 `image_jobs.PEAK_WINDOWS`（真实事实源），不另抄一份时段表——
    以后调时段只改那一处，这里跟着变。
    """
    from datetime import datetime as _dt
    from app.features.image import jobs as image_jobs
    hour = _dt.now().hour
    for start, end, label, window in image_jobs.PEAK_WINDOWS:
        if start <= hour < end:
            return True, ("%s（%s）排队最挤，一次只能跑一张。"
                          "过了这段时间再来批量。" % (label, window))
    return False, ""


def _parse_batch(body):
    """校验整单。→ (rows, err_msg, http_code)。

    rows 每项 {image, prompt, skill}。**整单先验完再动手**：半路才拒的话，
    前面几张已经入队了，用户看到的是「报了错，图却跑出来了」。
    """
    mode = str(body.get("mode") or "").strip()
    if mode not in ("same", "each"):
        return None, "出图方式不对", 400
    items = body.get("items")
    if not isinstance(items, list) or not items:
        return None, "没有要跑的图", 400
    prompt = str(body.get("prompt") or "").strip()
    if len(prompt) > _PROMPT_MAX_BATCH:
        return None, ("提示词太长（%d 字，最多 %d）"
                      % (len(prompt), _PROMPT_MAX_BATCH)), 400
    if mode == "same" and not prompt:
        return None, "写一句想画的内容再开跑", 400

    known = {c["id"] for c in _channel_rows()}
    rows, seen = [], set()
    for i, it in enumerate(items[: _BATCH_MAX + 1], 1):
        it = it if isinstance(it, dict) else {}
        skill = str(it.get("skill") or "").strip()
        image = str(it.get("image") or "").strip()
        # 五图各画里「没传图的格子」= 用户还没填，**跳过**而不是报错（传几张
        # 就跑几张）。渠道没选也算没填——这一格里反正什么都没有可画。
        if mode == "each" and not image:
            continue
        if not skill:
            return None, "第 %d 格没选渠道" % i, 400
        if skill not in known:
            return None, "没有「%s」这个渠道" % skill, 404
        # 重复渠道**只在一图多风里禁**（2026-11-25 用户拍板，两种模式两套规则）：
        # 一图多风的卖点是「同一张图看不同渠道的差异」，5 个格子撞同一个渠道等于
        # 白跑；五图各画是 5 张不同的图，用同一个渠道才是常态（「全部同第一款」
        # 按钮干的就是这事），这里必须放行。
        # 拒就**整单拒，不偷偷去重**——去重后用户以为 5 张都在跑，实际只跑 3 张，
        # 还查不出为什么。
        if mode == "same":
            if skill in seen:
                return None, ("「%s」选了两遍。一图多风每个格子要用不同渠道，"
                              "想看同一渠道的多张请用「五图各画」" % skill), 400
            seen.add(skill)
        if image:
            if not image.startswith("data:image/"):
                return None, "第 %d 格的图片格式不对，重新传" % i, 400
            if len(image) > _IMAGE_MAX:
                return None, "第 %d 格的图片太大了" % i, 400
        if not image and not prompt:
            return None, "第 %d 格没有图，也没有写需求" % i, 400
        rows.append({"image": image, "prompt": prompt, "skill": skill})

    if len(rows) > _BATCH_MAX:
        return None, "一次最多跑 %d 张" % _BATCH_MAX, 400
    if mode == "same":
        # 一图多风：一张参考图，多个渠道共用。有几个渠道就出几张。
        # 参考图**只发一份**（前端挂在第一项上，省请求体积），所以这里取
        # 「第一个带了图的项」，而不是死盯 rows[0]——第一格没选渠道时，图会在
        # 别的位置上。
        img = ""
        for r in rows:
            if r["image"]:
                img = r["image"]
                break
        if not img:
            return None, "先传一张参考图", 400
        rows = [{"image": img, "prompt": prompt, "skill": r["skill"]}
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

    流程：验令牌 → 整单校验（渠道/图片/重复渠道）→ 配额 → 起一个后台线程 →
    线程里**串行**逐张识图、**识完一张立刻入队一张**（不等后面几张）→ 全部
    入队后**发一条合并回执**（每张发一条会刷屏）。

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

    blocked, why = _batch_peak_blocked()
    if blocked:
        return jsonify({"ok": False, "error": why}), 429

    # 配额：按**真实张数**记账（这一单就是 5 张，不该只算 1 次）；而在途名额
    # 按「一整组」算（见 `_weigh`）——一个人开一批不该把全局 4 个名额吃光。
    ip = (request.headers.get("CF-Connecting-IP")
          or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
          or request.remote_addr or "?")
    if not _ip_ok(ip, cost=len(rows)):
        return jsonify({"ok": False, "error": "这一小时发太多次了，歇会儿"}), 429
    if _token_inflight(k) >= _PER_TOKEN_INFLIGHT:
        return jsonify({"ok": False,
                        "error": "你还有一批在跑，等它跑完再开下一批"}), 429
    if _inflight() >= _ALL_INFLIGHT:
        return jsonify({"ok": False, "error": "前面排队的人有点多，稍等再试"}), 429

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
                threading.Thread(target=_run, args=(
                    tag, k, parsed, "channel", item["skill"],
                    item["prompt"], item["image"]), daemon=True).start()
                sent += 1
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
        _send_batch_receipt(target, target_id, rows, sent, fails, bid)


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


def _send_batch_receipt(target, target_id, rows, sent, fails, bid):
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
        names = "、".join(r["skill"] for r in rows)
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
