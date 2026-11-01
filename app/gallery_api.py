# -*- coding: utf-8 -*-
"""画廊接口（挂在 5174 主进程，**不直接对公网**）。

和 `friend_api` 一样：只认 `X-Friend-Token` 头。5175 转发时带上，
浏览器直接打 5174 一定 403。

## 权限只有三档

| 谁 | 能干什么 |
|---|---|
| 登录过的人 | 看公开作品集、点赞、收藏、上传自己的 |
| 作者本人 | 另外能删自己的作品集（下架之后自己也还能看） |
| 站主 | 另外能扫描磁盘、看全部、一键下架 |

站主由 `GALLERY_OWNER_QQ` 认，**没配就等于没有站主**——宁可管理功能全关，
也不猜「谁可能是站主」。猜错的代价是这个站唯一的删除权限给错人。

## 为什么要 `full` 也走这个接口

直接返回磁盘路径当然更快，但那等于把 `D:\\Users\\...` 的结构送出去，
而且浏览器也读不到本地路径。所有图一律经这一层转发，只在 `send_file`
里读一次盘。
"""

import logging
import os
import time

from flask import Blueprint, jsonify, request, send_file

from app import friend_auth, gallery
from app.config import GALLERY_OWNER_QQ

log = logging.getLogger("gallery_api")

bp = Blueprint("gallery", __name__, url_prefix="/api/gal")

PAGE_MAX = 60
UPLOAD_MAX = 10                     # 一次最多传几张
UPLOAD_BYTES = 25 << 20             # 单张上限 25MB
UPLOAD_TOTAL = 250 << 20            # 一次合计上限 250MB


# ─── 公共小件 ────────────────────────────────────────────

def _guard():
    try:
        good = friend_auth.secret()
    except Exception:
        log.warning("拿不到内部密钥", exc_info=True)
        return jsonify({"ok": False, "error": "服务未就绪"}), 503
    got = str(request.headers.get("X-Friend-Token") or "")
    if not got or got != good:
        return jsonify({"ok": False, "error": "forbidden"}), 403
    return None


def _qq():
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        body = request.get_json(silent=True) or {}
        qq = str(body.get("qq") or "").strip()
    if not friend_auth.QQ_RE.match(qq):
        return None
    return qq


def _is_owner(qq):
    return bool(GALLERY_OWNER_QQ) and str(qq) == GALLERY_OWNER_QQ


def _page_args():
    try:
        page = max(1, int(request.args.get("page") or 1))
    except ValueError:
        page = 1
    try:
        size = int(request.args.get("size") or 24)
    except ValueError:
        size = 24
    return page, max(1, min(PAGE_MAX, size))


# ─── 列表 ────────────────────────────────────────────────

def _work_row(row, qq):
    """一行作品集 → 前端要的形状。别名在这里做，QQ 号绝不出接口。"""
    if row["src"] == "owner":
        name = "站长"
    elif GALLERY_OWNER_QQ and row["owner"] == GALLERY_OWNER_QQ:
        # 站主自己用网页传的也算站长作品，不然墙上会冒出一个「匿名用户 XX」
        # 和站主的 68 个目录并排，看着像两个人。
        name = "站长"
    else:
        name = gallery.alias(row["owner"])
    return {
        "id": row["id"],
        "title": row["title"] or "未命名",
        "grp": row["grp"] or "",
        "n": row["n"],
        "cover": row["cover_item"],
        "src": row["src"],
        "by": name,
        "mine": row["src"] == "user" and row["owner"] == str(qq),
        "hidden": bool(row["hidden"]),
    }


_SELECT_WORK = (
    "SELECT w.*, (SELECT i.id FROM items i WHERE i.work_id=w.id"
    " ORDER BY i.idx LIMIT 1) AS cover_item FROM works w")


@bp.route("/list")
def api_list():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400

    page, size = _page_args()
    where = ["w.n > 0"]
    args = []
    if not _is_owner(qq):
        # 下架的只有站主和作者本人能看到；公开墙一律过滤
        where.append("(w.hidden=0 OR (w.src='user' AND w.owner=?))")
        args.append(str(qq))

    # scope=owner → 站长的（磁盘目录 + 他自己用网页传的，口径同 _work_row）；
    # scope=pub   → 其他人的。**不传 scope 就还是全部**，老链接和测试不受影响。
    order = " ORDER BY (w.src='user') ASC, w.sort ASC, w.id DESC"
    scope = str(request.args.get("scope") or "").strip().lower()
    if scope == "owner":
        where.append("(w.src='owner' OR (w.src='user' AND w.owner=?))")
        args.append(str(GALLERY_OWNER_QQ or ""))
    elif scope == "pub":
        where.append("w.src='user' AND w.owner<>?")
        args.append(str(GALLERY_OWNER_QQ or ""))
        order = " ORDER BY w.id DESC"        # 公开墙看最新的，不care发现顺序

    grp = str(request.args.get("grp") or "").strip()
    if grp:
        where.append("w.grp=?")
        args.append(grp)

    sql = (_SELECT_WORK + " WHERE " + " AND ".join(where) +
           order + " LIMIT ? OFFSET ?")
    args += [size + 1, (page - 1) * size]

    with gallery.connect() as con:
        rows = con.execute(sql, args).fetchall()
        grps = [r["grp"] for r in con.execute(
            "SELECT DISTINCT grp FROM works WHERE n>0 AND grp<>''")]

    more = len(rows) > size
    return jsonify({"ok": True, "works": [_work_row(r, qq) for r in rows[:size]],
                    "grps": sorted(g for g in grps if g), "has_more": more,
                    "page": page, "owner": _is_owner(qq)})


@bp.route("/mine")
def api_mine():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    page, size = _page_args()
    with gallery.connect() as con:
        if _is_owner(qq):
            # 站主的「我的」必须包含他磁盘上那批目录（src='owner'）。
            # 只查 src='user' 的话这里是 0 条，而扫描按钮恰好挂在这个 tab 上，
            # 扫完报「13285 张图」却一张都不显示——站主会以为扫描没生效。
            rows = con.execute(
                _SELECT_WORK + " WHERE (w.src='user' AND w.owner=?)"
                " OR w.src='owner'"
                " ORDER BY (w.src='user') ASC, w.sort ASC, w.id DESC"
                " LIMIT ? OFFSET ?",
                (str(qq), size + 1, (page - 1) * size)).fetchall()
        else:
            rows = con.execute(
                _SELECT_WORK + " WHERE w.src='user' AND w.owner=?"
                " ORDER BY w.id DESC LIMIT ? OFFSET ?",
                (str(qq), size + 1, (page - 1) * size)).fetchall()
    return jsonify({"ok": True, "works": [_work_row(r, qq) for r in rows[:size]],
                    "has_more": len(rows) > size, "owner": _is_owner(qq),
                    "alias": "站长" if _is_owner(qq) else gallery.alias(qq)})


@bp.route("/work/<int:wid>")
def api_work(wid):
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400

    with gallery.connect() as con:
        row = con.execute(_SELECT_WORK + " WHERE w.id=?", (wid,)).fetchone()
        if not row:
            return jsonify({"ok": False, "error": "没有这个作品集"}), 404
        mine = row["src"] == "user" and row["owner"] == str(qq)
        if row["hidden"] and not mine and not _is_owner(qq):
            return jsonify({"ok": False, "error": "没有这个作品集"}), 404

        items = con.execute(
            "SELECT id, idx, path FROM items WHERE work_id=? ORDER BY idx",
            (wid,)).fetchall()
        rows = con.execute(
            "SELECT item_id, kind FROM marks WHERE qq=?"
            " AND item_id IN (SELECT id FROM items WHERE work_id=?)",
            (str(qq), wid)).fetchall()
        liked = {r["item_id"] for r in rows if r["kind"] == "like"}
        faved = {r["item_id"] for r in rows if r["kind"] == "fav"}
        likes = {r["item_id"]: r["c"] for r in con.execute(
            "SELECT item_id, COUNT(*) c FROM marks WHERE kind='like'"
            " AND item_id IN (SELECT id FROM items WHERE work_id=?)"
            " GROUP BY item_id", (wid,))}

    data = _work_row(row, qq)
    data["can_delete"] = mine
    data["can_hide"] = _is_owner(qq) and row["src"] == "owner"
    return jsonify({
        "ok": True,
        "work": data,
        "items": [{"id": r["id"], "idx": r["idx"],
                   "like": 1 if r["id"] in liked else 0,
                   "fav": 1 if r["id"] in faved else 0,
                   "likes": likes.get(r["id"], 0),
                   # 下载拿到的永远是压缩图（webp），不是源文件的格式。
                   # 这里写死而不是读 path 后缀：按 .png 命名一个 webp，
                   # 手机上点了打不开。
                   "ext": "webp"}
                  for r in items],
    })


# ─── 图 ──────────────────────────────────────────────────

@bp.route("/img/<int:iid>")
def api_img(iid):
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400

    with gallery.connect() as con:
        row = con.execute(
            "SELECT i.path, w.hidden, w.src, w.owner FROM items i"
            " JOIN works w ON w.id=i.work_id WHERE i.id=?", (iid,)).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "没这张图"}), 404
    if row["hidden"] and row["owner"] != str(qq) and not _is_owner(qq):
        return jsonify({"ok": False, "error": "没这张图"}), 404

    src = row["path"]
    if not os.path.isfile(src):
        return jsonify({"ok": False, "error": "文件不在了"}), 404

    # 画廊只发压缩图。原图中位 5MB、库里最大一张 149MB，一个作品集被完整
    # 拖一遍就是几个 GB 的上行；站主要原图直接开磁盘，不必过网页。
    # 传 `s=full` 也只会拿到预览图（不报错，免得旧链接碎掉）。
    mode = str(request.args.get("s") or "t").lower()

    # 群友上传的图落盘时就是 1600px WebP，直接发源文件：
    # 再压一次只掉画质，还白占一份缓存。
    if mode != "t" and gallery.reuse_direct(src):
        return send_file(src, mimetype="image/webp", conditional=True,
                         max_age=600)

    p = gallery.thumb(src, "t" if mode == "t" else "v")
    if not p:
        return jsonify({"ok": False, "error": "转码失败"}), 415
    return send_file(p, mimetype="image/webp", conditional=True, max_age=600)


# ─── 点赞 / 收藏 ─────────────────────────────────────────

@bp.route("/mark", methods=["POST"])
def api_mark():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    body = request.get_json(silent=True) or {}
    try:
        iid = int(body.get("item_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "参数不对"}), 400
    kind = str(body.get("kind") or "like").lower()
    if kind not in ("like", "fav"):
        return jsonify({"ok": False, "error": "参数不对"}), 400
    on = bool(body.get("on"))

    with gallery.connect() as con:
        if not con.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone():
            return jsonify({"ok": False, "error": "没这张图"}), 404
        if on:
            con.execute("INSERT OR IGNORE INTO marks(qq, item_id, kind, ts)"
                        " VALUES(?,?,?,?)", (str(qq), iid, kind, time.time()))
        else:
            con.execute("DELETE FROM marks WHERE qq=? AND item_id=? AND kind=?",
                        (str(qq), iid, kind))
        cnt = con.execute("SELECT COUNT(*) c FROM marks WHERE item_id=? AND kind=?",
                          (iid, kind)).fetchone()["c"]
    return jsonify({"ok": True, "on": on, "count": cnt})


def _marked(kind, qq, page, size):
    """「我收藏的 / 我点赞的」——按图（item）排，不按作品集。

    收藏和点赞只差一个 `kind`，两张表都不用建，走同一段 SQL。
    返回的每条带 `work` + `idx`：前端拿它直接开查看器并跳到那一张，
    不用为了看一张图先加载整个作品集。
    """
    where = ["m.qq=?", "m.kind=?", "w.n > 0"]
    args = [str(qq), kind]
    if not _is_owner(qq):
        # 下架的图别出现在「我收藏的」里（除了作者本人，他本来就还能看）
        where.append("(w.hidden=0 OR (w.src='user' AND w.owner=?))")
        args.append(str(qq))
    sql = ("SELECT i.id, i.work_id, i.idx, w.title, w.src, w.owner"
           " FROM marks m JOIN items i ON i.id=m.item_id"
           " JOIN works w ON w.id=i.work_id"
           " WHERE " + " AND ".join(where) +
           " ORDER BY m.ts DESC, i.id DESC LIMIT ? OFFSET ?")
    args += [size + 1, (page - 1) * size]
    with gallery.connect() as con:
        rows = con.execute(sql, args).fetchall()
    out = [{"id": r["id"], "work": r["work_id"], "idx": r["idx"],
            "title": r["title"] or "未命名",
            "by": "站长" if r["src"] == "owner" else gallery.alias(r["owner"])}
           for r in rows[:size]]
    return {"ok": True, "items": out, "has_more": len(rows) > size}


@bp.route("/favs")
def api_favs():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    page, size = _page_args()
    return jsonify(_marked("fav", qq, page, size))


@bp.route("/likes")
def api_likes():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    page, size = _page_args()
    return jsonify(_marked("like", qq, page, size))


# ─── 站主：选目录 / 扫描 / 下架 ─────────────────────────

@bp.route("/browse")
def api_browse():
    """列目录，给站主点选要扫描的文件夹。只有站主能看自己的磁盘结构。"""
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not _is_owner(qq):
        return jsonify({"ok": False, "error": "只有站主能选文件夹"}), 403
    out = gallery.browse(request.args.get("path") or "")
    if out.get("error"):
        return jsonify({"ok": False, "error": out["error"]}), 400
    out["ok"] = True
    return jsonify(out)


@bp.route("/scan", methods=["POST"])
def api_scan():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not _is_owner(qq):
        return jsonify({"ok": False,
                        "error": "没配 GALLERY_OWNER_QQ 或你不是站主"}), 403
    body = request.get_json(silent=True) or {}
    path = str(body.get("path") or "").strip()
    try:
        # 带 path 就只扫站主指定的这一个目录；不带就扫全部已配置的根。
        out = gallery.scan_path(path) if path else gallery.scan()
    except Exception as e:
        log.warning("扫描失败", exc_info=True)
        return jsonify({"ok": False, "error": "扫描出错：%s" % e}), 500
    if out.get("error"):
        return jsonify({"ok": False, "error": out["error"]}), 400
    return jsonify({"ok": True, "result": out})


@bp.route("/visible", methods=["POST"])
def api_visible():
    """下架 / 上架。只有站主能改。"""
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not _is_owner(qq):
        return jsonify({"ok": False, "error": "只有站主能改"}), 403
    body = request.get_json(silent=True) or {}
    try:
        wid = int(body.get("work_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "参数不对"}), 400
    hidden = 1 if body.get("hidden") else 0
    with gallery.connect() as con:
        con.execute("UPDATE works SET hidden=? WHERE id=?", (hidden, wid))
    return jsonify({"ok": True, "hidden": bool(hidden)})


@bp.route("/clean", methods=["POST"])
def api_clean():
    """清掉文件已经不在的条目（站主手工删完图之后点一下）。"""
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not _is_owner(qq):
        return jsonify({"ok": False, "error": "只有站主能改"}), 403
    return jsonify({"ok": True, "removed": gallery.drop_orphans()})


@bp.route("/work/<int:wid>", methods=["DELETE"])
def api_delete_work(wid):
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    ok, why = gallery.delete_work(wid, owner=qq)
    if not ok:
        return jsonify({"ok": False, "error": why}), 403
    return jsonify({"ok": True})


# ─── 群友上传 ────────────────────────────────────────────

@bp.route("/upload", methods=["POST"])
def api_upload():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400

    title = str(request.form.get("title") or "").strip()[:40]
    fs = [f for f in request.files.getlist("files") if f and f.filename]
    if not fs:
        return jsonify({"ok": False, "error": "没收到图片"}), 400
    if len(fs) > UPLOAD_MAX:
        return jsonify({"ok": False, "error": "一次最多传 %d 张" % UPLOAD_MAX}), 400

    payload, total = [], 0
    for f in fs:
        raw = f.read()
        if not raw:
            continue
        if len(raw) > UPLOAD_BYTES:
            return jsonify({"ok": False,
                            "error": "「%s」超过 25MB" % f.filename}), 400
        total += len(raw)
        if total > UPLOAD_TOTAL:
            return jsonify({"ok": False, "error": "一次别超过 300MB"}), 400
        if not gallery.load_upload_image(raw):
            return jsonify({"ok": False,
                            "error": "「%s」不是能读的图片" % f.filename}), 400
        payload.append((f.filename, raw))

    if not payload:
        return jsonify({"ok": False, "error": "没收到图片"}), 400

    wid, n = gallery.save_upload(qq, payload)
    if not wid:
        return jsonify({"ok": False, "error": "图片都转码失败了"}), 500
    with gallery.connect() as con:
        con.execute("UPDATE works SET title=? WHERE id=?",
                    (title or "未命名作品", wid))
    return jsonify({"ok": True, "work_id": wid, "n": n})


@bp.route("/upload/one", methods=["POST"])
def api_upload_one():
    """逐张上传：压好一张传一张。

    `wid` 空着就开一个新作品集并把 id 返回，后面几张带着同一个 wid 追加。
    这样第一张几秒内就能落地，不用等十张全压完；某一张失败也只影响它
    自己，不连累已经传上去的。
    """
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400

    title = str(request.form.get("title") or "").strip()[:40]
    f = (request.files.getlist("file") or [None])[0]
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "没收到图片"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"ok": False, "error": "空文件"}), 400
    if len(raw) > UPLOAD_BYTES:
        return jsonify({"ok": False,
                        "error": "「%s」超过 25MB" % f.filename}), 400
    if not gallery.load_upload_image(raw):
        return jsonify({"ok": False,
                        "error": "「%s」不是能读的图片" % f.filename}), 400

    try:
        wid = int(str(request.form.get("wid") or "0"))
    except ValueError:
        wid = 0
    if wid:
        mine, _ = gallery.own_work(qq, wid)
        if not mine:
            return jsonify({"ok": False, "error": "作品集不存在"}), 404
    else:
        wid = gallery.create_work(qq, title or "未命名作品")

    try:
        idx = gallery.append_upload(qq, wid, f.filename, raw)
    except Exception:
        log.warning("追加图片失败：%s", f.filename, exc_info=True)
        return jsonify({"ok": False,
                        "error": "这张转码失败，换一张试试"}), 500
    _, n = gallery.own_work(qq, wid)
    return jsonify({"ok": True, "work_id": wid, "n": n, "idx": idx})


@bp.route("/upload/abort", methods=["POST"])
def api_upload_abort():
    """一组图一张都没传成：把空壳作品集收回去，别留在墙上。"""
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    try:
        wid = int(str(request.form.get("wid") or "0"))
    except ValueError:
        wid = 0
    if not wid:
        return jsonify({"ok": True})
    return jsonify({"ok": True, "removed": gallery.abort_upload(qq, wid)})


@bp.route("/me")
def api_me():
    bad = _guard()
    if bad:
        return bad
    qq = _qq()
    if not qq:
        return jsonify({"ok": False, "error": "参数不对"}), 400
    return jsonify({"ok": True, "owner": _is_owner(qq),
                    "alias": "站长" if _is_owner(qq) else gallery.alias(qq)})
