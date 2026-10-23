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
  POST /api/output/delete           {"paths": ["web/生图页/miao", ...]}
                                     文件就删文件，目录就整棵删；返回释放字节

取图不另做端点——复用 main.py 的 `/api/image/<path:filename>`（先读本地磁盘，
没有才代理 ComfyUI `/view`）。

安全（删东西的接口，宁可多挡）：
  · 每条 path 都过 `_safe_abs()`：拆 `/` 与 `\\`、挡 `..`、join 后 realpath
    必须还在 output 根内，且不能是根本身（`commonpath` 校验，不同盘符会抛
    ValueError，一并挡掉）。
  · 删除逐个独立 try，一条失败不拦其余，汇总回报。
  · 前端另有二次确认弹窗（列出条目数），但确认只是体验，后端校验才是真闸门。
"""

import os
import threading
import time

from flask import Blueprint, jsonify, request

from app.config import COMFYUI_OUTPUT_DIR

bp = Blueprint("output_api", __name__)

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

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
            # 本层直接图片：套娃目录（qq/私聊_xxx/...）会显示 0，点进去再看
            n_img, n_bytes = 0, 0
            try:
                with os.scandir(ent.path) as sub:
                    for s in sub:
                        if s.is_file(follow_symlinks=False):
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
                         "n_img": n_img, "bytes": n_bytes, "mtime": mtime})
        elif ent.is_file(follow_symlinks=False):
            ext = os.path.splitext(ent.name)[1].lower()
            if ext not in IMG_EXT:
                continue                      # 只管理图片；其他文件不列不删
            files.append({"name": ent.name, "rel": _rel_of(ent.path),
                          "bytes": st.st_size, "mtime": mtime})

    dirs.sort(key=lambda d: d["name"])
    files.sort(key=lambda f: f["mtime"], reverse=True)
    parent = _rel_of(os.path.dirname(base)) if rel else ""
    if parent.startswith(".."):
        parent = ""
    return jsonify({"ok": True, "path": rel, "parent": parent,
                    "dirs": dirs, "files": files})


# ── 删除 ─────────────────────────────────────────────────
@bp.route("/api/output/delete", methods=["POST"])
def api_delete():
    """删文件或整目录。逐条独立 try，汇总回报；释放字节删前统计。"""
    body = request.get_json(silent=True) or {}
    paths = body.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return jsonify({"ok": False, "error": "paths 不能为空"}), 400
    if len(paths) > 200:
        return jsonify({"ok": False, "error": "一次最多删 200 条"}), 400

    deleted, failed, freed = [], [], 0
    for rel in paths:
        target = _safe_abs(rel)
        if not target:
            failed.append({"rel": rel, "error": "路径不合法或不存在"})
            continue
        try:
            freed += _tree_bytes(target)
            if os.path.isdir(target) and not os.path.islink(target):
                import shutil
                shutil.rmtree(target)
            else:
                os.remove(target)
            deleted.append(rel)
        except Exception as exc:                      # noqa: BLE001
            failed.append({"rel": rel, "error": str(exc)})

    # 让 /stat 立刻反映新账
    with _STAT_LOCK:
        _STAT["t"] = 0.0
    return jsonify({"ok": not failed, "deleted": deleted,
                    "failed": failed, "freed": freed,
                    "freed_human": _fmt_bytes(freed)})
