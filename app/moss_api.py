"""moss 漫画台 API（2026-10-21 用户拍板新建）。

和 `comic_api.py` **完全两套，互不调用**：那边是 Anima 的 tag 流水线（编剧 →
内容补正 → 逐格渲染 → qwen 加气泡），这边是 moss 的「一句话 → 编剧写整页 →
一次出图」。两边唯一共用的是 `image_jobs` 的出图队列。

为什么这边没有补正、没有逐格、没有后期贴字：

  · 补正治的是「漏 `1girl` / 犯 POV / 角色串味」这些 tag 硬规则。moss 的文本
    编码器是 Qwen3.5-9B，吃自然语言，没有这些规则——`moss_comic.check()` 只
    数格数、报字数，**只报不拦**。
  · 一页一次生成，格与格共享上下文，角色一致性天然成立；代价是改一格只能
    整页重抽（所以这里只有「整页换 seed」，没有逐格重渲）。
  · 气泡写进提示词由模型直接画（30 步实测 11 字内零错字），不需要另开一步。

端点：
  POST /api/moss/write    一句话剧情 → 编剧写 spec.json（同步，几秒）
  POST /api/moss/save     手改后的 spec 落盘（重算每页提示词）
  POST /api/moss/render   后台出图（整页；可指定页号、可换 seed）
  GET  /api/moss/status   轮询：spec + 体检 + 提示词 + 每页出图状态
  GET  /api/moss/img      出图（state.json 里记的相对 ComfyUI output 的路径）
  GET  /api/moss/list     历史任务

状态为什么不放内存：一张图 2~3 分钟，Flask 重启不能丢进度 → `state.json` 是
唯一事实来源，和 `comic_api` 一个道理。

⚠️ `image_jobs` 内部会 `raise SystemExit`（`_require_comfy` 等处），它不继承
Exception——线程里必须 `except BaseException`，否则失败是静默的，前端只会看到
永远转圈的「正在画」。
"""

import json
import os
import re
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app import image_jobs, moss_comic
from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR
from app.skills import load_workflow
from app.tools.normal.generate_image import apply_prompt_placeholders

bp = Blueprint("moss_api", __name__)

# 任务根目录钉死绝对路径：Flask 的启动 cwd 可能不是项目根。
STATE_ROOT = os.path.join(BASE_DIR, "state", "moss_comic")

# 出图参数（30 步是 10-21 拍板的漫画档：出片级才用 50 步）
SKILL = "moss50"
STEPS = 30
WIDTH, HEIGHT = 1024, 1536
SEED_BASE = 20261501
OUT_ROOT = "web/moss漫画"

# 运行中的任务：{任务目录绝对路径: {"t0": 起跑时间, "n": 当前页}}
_RUNNING = {}
_RUN_LOCK = threading.Lock()
# state.json 的读写互斥：后台线程写、HTTP 线程读，同一个文件。
_STATE_LOCK = threading.Lock()


# ══════════════════════════════════════════════════════════
#  任务目录 / 落盘
# ══════════════════════════════════════════════════════════
def _slug(text, limit=14):
    """标题 → 能当目录名的短串。中文保留（Windows 下没问题），只清非法字符。"""
    s = re.sub(r"[\\/:*?\"<>|\s]+", "", text or "")
    return (s or "untitled")[:limit]


def _new_task_dir(spec):
    """建任务目录 `<日期>_<时间>_<标题>`。同一分钟同名再加 `_2`，不静默复用。"""
    base = "%s_%s" % (time.strftime("%Y%m%d_%H%M"), _slug(spec.get("title")))
    d = os.path.join(STATE_ROOT, base)
    k = 2
    while os.path.isdir(d):
        d = os.path.join(STATE_ROOT, "%s_%d" % (base, k))
        k += 1
    os.makedirs(d, exist_ok=True)
    return d


def _task_dir(name):
    """任务名 → 绝对路径。只认 state/moss_comic 的**直接子目录**（挡路径穿越：
    `basename("..")` 还是 `..`，join 完就跑到上一级去了）。"""
    name = os.path.basename(str(name or "").strip())
    if not name or name in (".", ".."):
        raise ValueError("任务名不合法")
    d = os.path.realpath(os.path.join(STATE_ROOT, name))
    if os.path.dirname(d) != os.path.realpath(STATE_ROOT) or not os.path.isdir(d):
        raise ValueError("任务不存在：%s" % name)
    return d


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:                                   # noqa: BLE001
        return default


def _write_json(path, data):
    """先写 .tmp 再 os.replace——半截 JSON 会让轮询直接读到空任务。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _state_path(d):
    return os.path.join(d, "state.json")


def _legacy_files(d):
    """没有 state.json 的老任务（10-21 之前 CLI 跑的那几篇）的出图。

    CLI 的出图落在 `output/web/moss漫画/<同名目录>/moss50/`，文件名里带
    `_P01_`。页面得能把它们显示出来，否则用户之前跑的五篇打开全是空白。
    """
    if not COMFYUI_OUTPUT_DIR:
        return {}
    folder = os.path.join(COMFYUI_OUTPUT_DIR, OUT_ROOT.replace("/", os.sep),
                          os.path.basename(d), SKILL)
    if not os.path.isdir(folder):
        return {}
    out = {}
    for f in sorted(os.listdir(folder)):
        m = re.search(r"_P0*(\d+)_", f)
        if m and os.path.splitext(f)[1].lower() in (".png", ".jpg", ".jpeg", ".webp"):
            out[int(m.group(1))] = "/".join([OUT_ROOT, os.path.basename(d), SKILL, f])
    return out


def _page_file(d, n, pages=None):
    """这一页的图（相对 ComfyUI output）。先认 state.json，再认老目录。"""
    pages = pages if pages is not None else (_load_state(d).get("pages") or {})
    rel = (pages.get(str(n)) or {}).get("file") or ""
    return rel or _legacy_files(d).get(int(n), "")


def _load_state(d):
    return _read_json(_state_path(d), {})


def _set_page(d, n, **kw):
    with _STATE_LOCK:
        st = _read_json(_state_path(d), {})
        pages = st.setdefault("pages", {})
        pages.setdefault(str(n), {}).update(kw)
        st["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _write_json(_state_path(d), st)


def _normalize(spec):
    """把前端送来的 spec 收拾成能拼提示词的样子。

    **不做准入判断**——只补默认值、去空白、重编号。格数对不对由
    `moss_comic.check()` 出报告，页面自己显示，不在这里拒绝。
    """
    spec = spec if isinstance(spec, dict) else {}
    cast = spec.get("cast") or {}
    spec["cast"] = {str(k).strip(): str(v).strip()
                    for k, v in cast.items() if str(k).strip()}
    spec["style"] = str(spec.get("style") or "").strip()
    spec["title"] = str(spec.get("title") or "").strip() or "未命名"
    pages = []
    for i, page in enumerate(spec.get("pages") or [], 1):
        if not isinstance(page, dict):
            continue
        page["n"] = i
        page["layout"] = str(page.get("layout") or "single").strip()
        page["scene"] = str(page.get("scene") or "").strip()
        panels = []
        for panel in page.get("panels") or []:
            if not isinstance(panel, dict):
                continue
            panels.append({k: str(panel.get(k) or "").strip()
                           for k in ("desc", "bubble", "sfx", "caption")})
        page["panels"] = panels
        pages.append(page)
    spec["pages"] = pages
    return spec


def _prompts(spec):
    """{页号字符串: 最终提示词}。"""
    out = {}
    for page in spec.get("pages") or []:
        out[str(page.get("n"))] = moss_comic.build_page_prompt(spec, page)
    return out


def _save(d, spec, raw=None):
    """spec.json / prompts.txt 落盘（spec_raw.txt 只在初次编剧时写）。"""
    spec = _normalize(spec)
    with _STATE_LOCK:
        _write_json(os.path.join(d, "spec.json"), spec)
        if raw is not None:
            with open(os.path.join(d, "spec_raw.txt"), "w", encoding="utf-8") as fh:
                fh.write(raw)
    ps = _prompts(spec)
    with open(os.path.join(d, "prompts.txt"), "w", encoding="utf-8") as fh:
        for page in spec.get("pages") or []:
            n = page.get("n")
            fh.write("── 第 %s 页 [%s] %d 格\n%s\n\n"
                     % (n, page.get("layout"), len(page.get("panels") or []),
                        ps.get(str(n), "")))
    return spec, ps


# ══════════════════════════════════════════════════════════
#  组装返回体
# ══════════════════════════════════════════════════════════
def _running(d):
    with _RUN_LOCK:
        r = _RUNNING.get(d)
        if not r:
            return None
        return {"page": r.get("n"),
                "waited": round(time.time() - r.get("t0", time.time()), 1)}


def _payload(d, spec=None, prompts=None):
    if spec is None:
        spec = _read_json(os.path.join(d, "spec.json"), {})
    if prompts is None:
        prompts = _prompts(spec)
    st = (_load_state(d).get("pages") or {})
    legacy = _legacy_files(d)
    task = os.path.basename(d)
    pages = []
    for page in spec.get("pages") or []:
        n = page.get("n")
        s = st.get(str(n)) or {}
        rel = s.get("file") or legacy.get(int(n)) or ""
        status = s.get("status") or ("done" if rel else "pending")
        item = dict(page)
        item.update({
            "prompt": prompts.get(str(n), ""),
            "status": status,
            "seed": s.get("seed"),
            "secs": s.get("secs"),
            "error": s.get("error") or "",
            # 带 v= 是为了换 seed 后浏览器不复用旧图缓存
            "url": ("/api/moss/img?task=%s&n=%s&v=%s"
                    % (task, n, s.get("rev") or s.get("seed") or 0))
                   if rel else "",
        })
        pages.append(item)
    return {
        "ok": True,
        "task": task,
        "title": spec.get("title"),
        "style": spec.get("style"),
        "cast": spec.get("cast") or {},
        "ratio": "%dx%d" % (WIDTH, HEIGHT),
        "steps": STEPS,
        "running": _running(d),
        "issues": moss_comic.check(spec),
        "pages": pages,
    }


# ══════════════════════════════════════════════════════════
#  编剧
# ══════════════════════════════════════════════════════════
@bp.route("/api/moss/write", methods=["POST"])
def api_moss_write():
    """一句话剧情 → 整篇 spec。同步跑：实测 deepseek-flash 5 秒，够快。

    返回的 `task` 是新建的任务目录名，后面 save / render / status 都靠它。
    """
    data = request.get_json(silent=True) or {}
    brief = str(data.get("brief") or "").strip()
    if not brief:
        return jsonify({"ok": False, "error": "先写一句剧情"}), 400
    try:
        pages = max(1, min(12, int(data.get("pages") or 1)))
    except (TypeError, ValueError):
        pages = 1
    spec, raw, err = moss_comic.write(
        brief, pages=pages,
        style=str(data.get("style") or "").strip(),
        ratio="%dx%d" % (WIDTH, HEIGHT),
        layout=str(data.get("layout") or "").strip())
    if spec is None:
        # 502 = 模型侧没交作业（解析失败），不是用户的错，页面提示重来一次
        return jsonify({"ok": False,
                        "error": "编剧没写出能解析的剧本：%s" % err,
                        "raw": (raw or "")[:1500]}), 502
    d = _new_task_dir(spec)
    spec, ps = _save(d, spec, raw)
    out = _payload(d, spec, ps)
    out["brief"] = brief
    return jsonify(out)


@bp.route("/api/moss/save", methods=["POST"])
def api_moss_save():
    """手改后的 spec 落盘。改完提示词会重算，页面拿新的 prompt 预览。"""
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    spec = data.get("spec")
    if not isinstance(spec, dict):
        return jsonify({"ok": False, "error": "spec 不是对象"}), 400
    spec, ps = _save(d, spec)
    return jsonify(_payload(d, spec, ps))


# ══════════════════════════════════════════════════════════
#  出图
# ══════════════════════════════════════════════════════════
def _seed_for(d, n, given):
    """定这一页的种子：给了就用；否则沿用上次的（重画同一张）；再否则默认。"""
    if given:
        return int(given)
    st = (_load_state(d).get("pages") or {}).get(str(n)) or {}
    return int(st.get("seed") or (SEED_BASE + n - 1))


def _draw(page, prompt, base_wf, seed, out_dir, title):
    """一页 → 一张图。返回 (相对 ComfyUI output 的路径 or None, 错误串)。"""
    wf = json.loads(json.dumps(base_wf))
    wf["30"]["inputs"]["width"] = WIDTH
    wf["30"]["inputs"]["height"] = HEIGHT
    wf["40"]["inputs"]["steps"] = STEPS
    n = page.get("n")
    wf["60"]["inputs"]["filename_prefix"] = "MossComic_%s_P%02d" % (_slug(title), n)
    wf = apply_prompt_placeholders(json.dumps(wf, ensure_ascii=False), prompt, seed)

    job, why = image_jobs.enqueue(None, None, wf, skill=SKILL, prompt=prompt[:200],
                                  seed=seed, out_dir=out_dir)
    if job is None:
        return None, why or "队列没接"
    try:
        job.wait()
    except BaseException as exc:                # noqa: BLE001  见文件头 ⚠️
        return None, str(exc) or exc.__class__.__name__
    files = image_jobs.output_images(job.entry, job=job)
    if not files:
        return None, "跑完了但没找到图"
    # Windows 上 output_images 拼出来是反斜杠，统一成 `/` 存盘，
    # 免得 state.json 里出现 `web\moss漫画\xxx/moss50/` 这种混合分隔符
    return files[0].replace("\\", "/"), ""


def _run(d, spec, nums, seed_base):
    """后台线程：逐页出图，每页状态落 state.json（页面靠它轮询）。"""
    title = spec.get("title") or "未命名"
    try:
        base_wf = load_workflow(os.path.join(BASE_DIR, "skills", SKILL, "workflow.json"))
        if not base_wf:
            raise RuntimeError("skills/%s/workflow.json 读不出来" % SKILL)
        prompts = _prompts(spec)
        by_n = {p.get("n"): p for p in spec.get("pages") or []}
        out_dir = "%s/%s" % (OUT_ROOT, os.path.basename(d))
        for n in nums:
            page = by_n.get(n)
            if not page:
                continue
            with _RUN_LOCK:
                if d in _RUNNING:
                    _RUNNING[d]["n"] = n
            seed = _seed_for(d, n, seed_base)
            _set_page(d, n, status="running", error="", rev=int(time.time()))
            t0 = time.time()
            try:
                rel, err = _draw(page, prompts.get(str(n), ""), base_wf, seed,
                                 out_dir, title)
            except BaseException as exc:        # noqa: BLE001
                rel, err = None, str(exc) or exc.__class__.__name__
            if rel:
                _set_page(d, n, status="done", file=rel, seed=seed,
                          secs=round(time.time() - t0, 1), error="",
                          rev=int(time.time()))
            else:
                _set_page(d, n, status="error", error=err or "没出图", seed=seed,
                          secs=round(time.time() - t0, 1), rev=int(time.time()))
    except BaseException as exc:                # noqa: BLE001
        for n in nums:
            st = (_load_state(d).get("pages") or {}).get(str(n)) or {}
            if st.get("status") != "done":
                _set_page(d, n, status="error",
                          error=str(exc) or exc.__class__.__name__)
    finally:
        with _RUN_LOCK:
            _RUNNING.pop(d, None)


@bp.route("/api/moss/render", methods=["POST"])
def api_moss_render():
    """后台出图。`pages` 不传＝全部；`seed` 明确给就用，`reroll=true` 换随机。

    同一任务同时只跑一个线程——两批一起跑会在 `image_jobs` 队列里抢同一套
    模型，白等两遍冷启动。
    """
    data = request.get_json(silent=True) or {}
    try:
        d = _task_dir(data.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    spec = _read_json(os.path.join(d, "spec.json"), {})
    all_n = [p.get("n") for p in spec.get("pages") or []]
    if not all_n:
        return jsonify({"ok": False, "error": "剧本是空的"}), 400

    want = data.get("pages")
    nums = all_n
    if isinstance(want, list) and want:
        keep = set()
        for x in want:
            try:
                keep.add(int(x))
            except (TypeError, ValueError):
                pass
        nums = [n for n in all_n if n in keep]
    if not nums:
        return jsonify({"ok": False, "error": "没有要画的页"}), 400

    try:
        seed_base = int(data.get("seed") or 0)
    except (TypeError, ValueError):
        seed_base = 0
    if data.get("reroll") and not seed_base:
        seed_base = int(time.time() * 1000) % 900000000 + 100000000

    with _RUN_LOCK:
        if _RUNNING.get(d):
            return jsonify({"ok": False, "error": "这个任务正在画，等它画完"}), 409
        _RUNNING[d] = {"t0": time.time(), "n": nums[0]}

    threading.Thread(target=_run, args=(d, spec, nums, seed_base), daemon=True,
                     name="moss-comic-%s" % os.path.basename(d)).start()
    return jsonify({"ok": True, "task": os.path.basename(d), "pages": nums,
                    "seed": seed_base or None})


# ══════════════════════════════════════════════════════════
#  读：状态 / 图 / 历史
# ══════════════════════════════════════════════════════════
@bp.route("/api/moss/status", methods=["GET"])
def api_moss_status():
    try:
        d = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    return jsonify(_payload(d))


@bp.route("/api/moss/img", methods=["GET"])
def api_moss_img():
    """出图。只认 state.json 里记过的那条相对路径，不做通用静态目录服务。"""
    try:
        d = _task_dir(request.args.get("task"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    try:
        n = int(request.args.get("n"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "n 不是数字"}), 400
    rel = _page_file(d, n)
    if not rel:
        return jsonify({"ok": False, "error": "这一页还没出图"}), 404
    full = rel if os.path.isabs(rel) else os.path.join(
        COMFYUI_OUTPUT_DIR or "", rel.replace("/", os.sep))
    if not os.path.isfile(full):
        return jsonify({"ok": False, "error": "图不见了：%s" % rel}), 404
    return send_file(full, max_age=0)


@bp.route("/api/moss/list", methods=["GET"])
def api_moss_list():
    """历史任务（按 spec.json 的修改时间倒序）。"""
    items = []
    if os.path.isdir(STATE_ROOT):
        for name in os.listdir(STATE_ROOT):
            d = os.path.join(STATE_ROOT, name)
            sp = os.path.join(d, "spec.json")
            if not os.path.isfile(sp):
                continue
            spec = _read_json(sp, {})
            pages = spec.get("pages") or []
            st = _load_state(d).get("pages") or {}
            have = [(p.get("n"), _page_file(d, p.get("n"), st)) for p in pages]
            have = [(n, rel) for n, rel in have if rel]
            first = have[0] if have else (None, "")
            items.append({
                "task": name,
                "title": spec.get("title") or name,
                "pages": len(pages),
                "layout": (pages[0].get("layout") if pages else "") or "",
                "done": len(have),
                "mtime": time.strftime("%m-%d %H:%M",
                                       time.localtime(os.path.getmtime(sp))),
                "thumb": ("/api/moss/img?task=%s&n=%s" % (name, first[0]))
                         if first[1] else "",
                "_ts": os.path.getmtime(sp),
            })
    items.sort(key=lambda x: x.get("_ts") or 0, reverse=True)
    for it in items:
        it.pop("_ts", None)
    return jsonify({"ok": True, "items": items})
