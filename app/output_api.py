"""output 图片管理台 API（2026-10-23 用户提的需求）。

动机：用户要翻一张图得在资源管理器里逐层点 `output/web/生图页/<渠道>/`，
「一个一个打开文件夹去找，真的很麻烦」。这个蓝图把 ComfyUI 的 output 目录
搬上网页：左边目录树点开就看，右边缩略图网格，勾选即删，整目录一键删。

和 `moss_api` 的分工：那边只管 `state/moss_comic` 的任务态；这边是**纯磁盘
视图**，不读任何 state.json，ComfyUI 往 output 里写的任何东西这里都能看到
（web 生图页、qq 私聊图、MOSS-Image 测试图、漫画成品）。

端点：
  GET  /api/output/tree?path=<rel>   列一层目录：子目录（带本层图片数/占用）
                                     + 本层图片文件。path 空 = output 根
  GET  /api/output/stat              output 总占用（缓存 60s，全盘 walk 一次）
  GET  /api/output/thumb?path=&w=    网格用缩略图（JPEG，落盘缓存在
                                     state/output_thumbs/，键含 mtime+size）
  POST /api/output/delete           {"paths": ["web/生图页/miao", ...]}
                                     文件就删文件，目录就整棵删；**移进回收站**
                                     （state/output_trash/，保留原目录结构），
                                     不真删磁盘，挪回去就能恢复

取图：网格走 `/api/output/thumb`（一屏 18 张原图 150 MB，必卡）；灯箱走
main.py 的 `/api/image/<path:filename>`（先读本地磁盘，没有才代理 ComfyUI
`/view`），这样看大图不丢质量。

安全（删东西的接口，宁可多挡）：
  · 每条 path 都过 `_safe_abs()`：拆 `/` 与 `\\`、挡 `..`、join 后 realpath
    必须还在 output 根内，且不能是根本身（`commonpath` 校验，不同盘符会抛
    ValueError，一并挡掉）。
  · 删除逐个独立 try，一条失败不拦其余，汇总回报。
  · 前端另有二次确认弹窗（列出条目数），但确认只是体验，后端校验才是真闸门。
"""

import hashlib
import json
import os
import re
import shutil
import threading
import time

from flask import Blueprint, jsonify, request, send_file

from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR

bp = Blueprint("output_api", __name__)

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# 缩略图缓存。**这是页面不卡的关键**：一张 1088x1920 的 PNG 是 6~12 MB，
# 一屏 18 张就是 150 MB，浏览器要解码 18 次全尺寸位图——网格滚动必然卡。
# 走这里出 320px 的 JPEG 只有 15~25 KB，秒开；原图只在灯箱里加载。
# 缓存键含 mtime+size，图被覆盖/删掉后自然失效（不做过期清理，缓存量级很小）。
THUMB_DIR = os.path.join(BASE_DIR, "state", "output_thumbs")
THUMB_SIZES = {160, 240, 320, 480}          # 只认这几档，别当万能转码器
_THUMB_LOCK = threading.Lock()

# ── output 根 ────────────────────────────────────────────
def _root():
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    if not root:
        raise ValueError("COMFYUI_OUTPUT_DIR 未配置")
    return os.path.abspath(root)


def _safe_abs(rel):
    """相对路径 → output 根内的绝对路径；越界/不存在返回 None。

    和 main.py `_local_output_file` 同一套校验：输出路径全是我们自己写出去
    的，出现 `..` 一律不认。**删除接口的生命线**——`rel` 直接来自 HTTP body。
    """
    try:
        root = _root()
    except ValueError:
        return None
    parts = [x for x in str(rel or "").replace("\\", "/").split("/") if x]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    path = os.path.abspath(os.path.join(root, *parts))
    try:
        if os.path.commonpath([path, root]) != root or path == root:
            return None
    except ValueError:                       # 不同盘符
        return None
    return path if os.path.exists(path) else None


def _rel_of(path):
    root = os.path.abspath((COMFYUI_OUTPUT_DIR or "").strip())
    return os.path.relpath(path, root).replace("\\", "/")


def _fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%d B" % n


def _natural_key(name):
    """文件名自然序。ComfyUI 出图名是 `QwenImage_00005_.png`，纯字典序会把
    00010 排到 0009 前面（'0' < '9'），所以按「文本段 / 数字段」拆开比较。

    每段返回 `(类型, 数字, 文本)` 三元组而不是裸 `int`/`str`：同一位置可能
    一边是数字一边是文本（`abc_1` vs `abc_x`），Python 3 直接比会 TypeError。
    数字段用 `(0, n, "")` 排在前，文本段 `(1, 0, s)` 比较时忽略大小写。
    """
    out = []
    for tok in re.split(r"(\d+)", str(name or "")):
        if not tok:
            continue
        if tok.isdigit():
            out.append((0, int(tok), ""))
        else:
            out.append((1, 0, tok.lower()))
    return out


def _tree_bytes(path):
    """整棵目录的字节数（只在删除前算一次，不缓存）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    return total


# ── 回收站 ───────────────────────────────────────────────
# 2026-10-30 用户要求：「删掉瑕疵图的时候，二次确认后丢进回收站」。
# 之前这里是真的 os.remove / shutil.rmtree —— 鼠标一点图就没了，误删无解。
#
# 放在 `state/output_trash/` 而不是 output 里面，两个原因：
#   · output 里的任何目录都会被 `/api/output/tree` 当成子目录列出来，
#     回收站混进去等于自己污染自己的目录树；
#   · ComfyUI 自己也会扫 output，一堆待删的图搁在里面容易被误当成产出。
#
# **保留原目录结构**（output/web/生图页/x.png → trash/web/生图页/x.png），
# 不按日期建批次目录：恢复的时候要的是「这张图原来在哪」，多一层日期只是
# 让人多点两下。想撤销就直接把 trash 里那层拖回 output，不用查任何记录。
TRASH_ROOT = os.path.join(BASE_DIR, "state", "output_trash")
_TRASH_LOG = os.path.join(TRASH_ROOT, "_deleted.jsonl")


def _unique_dest(dest):
    """回收站里已有同名项时加 `-1`、`-2` 后缀。

    **绝不覆盖**：覆盖等于换个位置真删一次，那这层回收站就白建了。
    目录也走这条路（`os.path.splitext` 对目录名只会切出空扩展，名字整段保留）。
    """
    if not os.path.exists(dest):
        return dest
    stem, ext = os.path.splitext(dest)
    n = 1
    while True:
        cand = "%s-%d%s" % (stem, n, ext)
        if not os.path.exists(cand):
            return cand
        n += 1


def _move_to_trash(rel, target):
    """把 output 里的一项挪进回收站，返回回收站里的绝对路径。"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise ValueError("路径不合法：%r" % (rel,))

    dest = os.path.abspath(os.path.join(TRASH_ROOT, *parts))
    # 第二道保险：`rel` 进来前已过 `_safe_abs`，这里再钉一次「必须落在 trash 内」。
    # 回收站是文件系统里真实可写的目录，拼错一次就是往外写。
    try:
        if os.path.commonpath([dest, os.path.abspath(TRASH_ROOT)]) != os.path.abspath(TRASH_ROOT):
            raise ValueError("目标越界：%r" % (rel,))
    except ValueError:
        raise ValueError("目标越界：%r" % (rel,))

    os.makedirs(os.path.dirname(dest), exist_ok=True)
    dest = _unique_dest(dest)
    # `shutil.move` 同盘走 rename、跨盘自动 copy+删源；
    # 不能用 `os.replace`——trash 和 output 不在同一个盘符时会直接抛异常。
    shutil.move(target, dest)
    return dest


def _log_trash(rel, dest, size):
    """往 `_deleted.jsonl` 追一行删除记录（时间 + 原路径 + 大小）。

    只是给人看「什么时候删的」，恢复不依赖它。写失败不影响删除本身——
    记录丢了图还在，反过来才是灾难。
    """
    try:
        os.makedirs(TRASH_ROOT, exist_ok=True)
        row = {"ts": round(time.time(), 3),
               "rel": rel,
               "trash": os.path.relpath(dest, TRASH_ROOT).replace("\\", "/"),
               "bytes": size}
        with open(_TRASH_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:                                # noqa: BLE001
        pass


# ── 总占用缓存 ───────────────────────────────────────────
_STAT = {"t": 0.0, "data": None}
_STAT_LOCK = threading.Lock()
_STAT_TTL = 60.0


@bp.route("/api/output/stat")
def api_stat():
    """output 总占用。全盘 walk 一次（实测几千文件 <1s），60s 缓存。"""
    now = time.time()
    with _STAT_LOCK:
        if _STAT["data"] and now - _STAT["t"] < _STAT_TTL:
            return jsonify(_STAT["data"])
    try:
        root = _root()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    n_files = 0
    n_dirs = 0
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        n_dirs += len(dirnames)
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
                n_files += 1
            except OSError:
                pass
    data = {"ok": True, "root": root, "files": n_files, "dirs": n_dirs,
            "bytes": total, "human": _fmt_bytes(total)}
    with _STAT_LOCK:
        _STAT["t"] = time.time()
        _STAT["data"] = data
    return jsonify(data)


# ── 目录树 ───────────────────────────────────────────────
@bp.route("/api/output/tree")
def api_tree():
    """列一层。子目录只报**本层直接**的图片数/占用（不递归）：qq 下面 11G，
    递归统计会让每次点击都卡秒级，而用户点进去自然能看到下一层。"""
    rel = (request.args.get("path") or "").strip()
    try:
        root = _root()
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    if rel:
        base = _safe_abs(rel)
        if not base or not os.path.isdir(base):
            return jsonify({"ok": False, "error": "目录不存在：%s" % rel}), 404
    else:
        base = root
        rel = ""

    dirs, files = [], []
    try:
        with os.scandir(base) as it:
            entries = list(it)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    for ent in entries:
        try:
            st = ent.stat()
        except OSError:
            continue
        mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))
        if ent.is_dir(follow_symlinks=False):
            # 本层直接图片：套娃目录（qq/私聊_xxx/...）会显示 0，点进去再看。
            # 顺手数本层直接子目录数：没有直接图片的中间层（web、生图页）光看
            # 「0P · 0 B」会被当成空目录，前端拿这个数改成显示「N 个目录」。
            n_img, n_bytes, n_sub = 0, 0, 0
            try:
                with os.scandir(ent.path) as sub:
                    for s in sub:
                        if s.is_dir(follow_symlinks=False):
                            n_sub += 1
                        elif s.is_file(follow_symlinks=False):
                            ext = os.path.splitext(s.name)[1].lower()
                            if ext in IMG_EXT:
                                n_img += 1
                                try:
                                    n_bytes += s.stat().st_size
                                except OSError:
                                    pass
            except OSError:
                pass
            dirs.append({"name": ent.name, "rel": _rel_of(ent.path),
                         "n_img": n_img, "n_dirs": n_sub,
                         "bytes": n_bytes, "mtime": mtime})
        elif ent.is_file(follow_symlinks=False):
            ext = os.path.splitext(ent.name)[1].lower()
            if ext not in IMG_EXT:
                continue                      # 只管理图片；其他文件不列不删
            files.append({"name": ent.name, "rel": _rel_of(ent.path),
                          "bytes": st.st_size, "mtime": mtime})

    # 按编号升序（自然序）。原来是 mtime 倒序，网格里 00005 排在最左，用户
    # 找图得倒着看；编号顺序才是出图顺序。
    dirs.sort(key=lambda d: _natural_key(d["name"]))
    files.sort(key=lambda f: _natural_key(f["name"]))
    parent = _rel_of(os.path.dirname(base)) if rel else ""
    if parent.startswith(".."):
        parent = ""
    return jsonify({"ok": True, "path": rel, "parent": parent,
                    "dirs": dirs, "files": files})


# ── 缩略图 ───────────────────────────────────────────────
@bp.route("/api/output/thumb")
def api_thumb():
    """出网格用的小图（JPEG），首次生成后落盘缓存。

    `w` 只认 THUMB_SIZES 里那几档；不传按 320。原图走 /api/image（灯箱用）。
    损坏图/不支持格式 → 404，前端 onerror 回退到原图地址，不至于整格空白。
    """
    from PIL import Image

    target = _safe_abs(request.args.get("path"))
    if not target or not os.path.isfile(target):
        return jsonify({"ok": False, "error": "图不存在"}), 404
    if os.path.splitext(target)[1].lower() not in IMG_EXT:
        return jsonify({"ok": False, "error": "不是图片"}), 404
    try:
        w = int(request.args.get("w") or 320)
    except (TypeError, ValueError):
        w = 320
    if w not in THUMB_SIZES:
        w = 320

    try:
        st = os.stat(target)
    except OSError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 404
    key = "%s_%d_%d_%d" % (hashlib.md5(target.encode("utf-8")).hexdigest()[:16],
                           int(st.st_mtime), st.st_size, w)
    cache = os.path.join(THUMB_DIR, key + ".jpg")
    if os.path.isfile(cache):
        return send_file(cache, mimetype="image/jpeg", max_age=86400, conditional=True)

    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
        with Image.open(target) as im:
            im = im.convert("RGB")            # PNG 带 alpha → JPEG 不支持
            # 长边封顶，等比缩放；竖图按 2 倍高算，够网格 1:1 裁剪用
            im.thumbnail((w, w * 3), Image.LANCZOS)
            tmp = cache + ".%d.tmp" % os.getpid()
            with _THUMB_LOCK:                 # 同一张图并发首访只写一次
                im.save(tmp, "JPEG", quality=82, optimize=True)
                os.replace(tmp, cache)
    except Exception as exc:                  # noqa: BLE001
        return jsonify({"ok": False, "error": "缩略图失败：%s" % exc}), 500
    return send_file(cache, mimetype="image/jpeg", max_age=86400, conditional=True)


# ── 删除（移入回收站）─────────────────────────────────────
@bp.route("/api/output/delete", methods=["POST"])
def api_delete():
    """把文件或整目录挪进回收站。逐条独立 try，汇总回报。

    返回值里 `moved_bytes` 是**本次挪出 output 的字节数**，不是「磁盘腾出了
    多少」——图还在 `state/output_trash/` 里占着，真要腾空间得清空回收站。
    旧字段 `freed` / `freed_human` 保留同名同义（= moved_bytes），免得别处
    引用旧响应的地方拿到 undefined。
    """
    body = request.get_json(silent=True) or {}
    paths = body.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return jsonify({"ok": False, "error": "paths 不能为空"}), 400
    if len(paths) > 200:
        return jsonify({"ok": False, "error": "一次最多删 200 条"}), 400

    moved, failed, moved_bytes = [], [], 0
    for rel in paths:
        target = _safe_abs(rel)
        if not target:
            failed.append({"rel": rel, "error": "路径不合法或不存在"})
            continue
        try:
            size = _tree_bytes(target) if os.path.isdir(target) else os.path.getsize(target)
            dest = _move_to_trash(rel, target)
            _log_trash(rel, dest, size)
            moved.append(rel)
            moved_bytes += size
        except Exception as exc:                      # noqa: BLE001
            failed.append({"rel": rel, "error": str(exc)})

    # 让 /stat 立刻反映新账
    with _STAT_LOCK:
        _STAT["t"] = 0.0
    human = _fmt_bytes(moved_bytes)
    return jsonify({"ok": not failed,
                    "deleted": moved,          # 旧名，前端一直在用
                    "moved": moved,
                    "failed": failed,
                    "moved_bytes": moved_bytes,
                    "moved_human": human,
                    "freed": moved_bytes,      # 兼容旧调用方
                    "freed_human": human})
