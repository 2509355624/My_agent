"""角色扮演的「查看台」接口（2026-11-03）。

## 干什么

给站主看：**谁玩过角色扮演、聊了什么**。

只挂在 5174，而且 5174 不对外（公网只映射 5175 的网关），所以不需要额外
鉴权——和 `admin` / `comic` 那些管理页同一个口径。

## 数据有两个来源

    登录 / 网页    agents/qq/sessions/persona_<key>.jsonl
    游客           agents/qq/游客-角色扮演/<日期>/<uuid>.jsonl

游客按天分目录，同一个 uuid 可能散在多天的文件里，读的时候要合并。

## 会话键长什么样

    private_<QQ>              QQ 私聊
    group_<群号>_<QQ>         群聊（2026-11-03 起不再实际扮演，这是历史数据）
    group_<群号>              群聊早期版本（没按人隔离）
    web_<QQ>                  网页端登录用户
    web_local                 网页端未传身份
    <uuid>                    游客
"""
import glob
import json
import logging
import os

from flask import Blueprint, jsonify, request

from app import chat_bg, guest, persona, persona_rag
from app.config import BASE_DIR

log = logging.getLogger("persona_admin")

bp = Blueprint("persona_admin", __name__)

SESS_DIR = os.path.join(BASE_DIR, "agents", "qq", "sessions")


def _norm(msgs):
    """把一份历史拆成展示用的消息列表（剥掉三行元数据）。"""
    out = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        body = m.get("content") or ""
        if m.get("role") == "user":
            out.append({"role": "user", "text": body})
        else:
            reply, _scene, mood, intensity, state = persona.split_turn(body)
            if reply.strip():
                out.append({"role": "assistant", "text": reply,
                            "mood": mood, "intensity": intensity,
                            "state": state})
    return out


def _read_jsonl(path):
    msgs = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(json.loads(line))
                except ValueError:
                    pass
    except OSError:
        log.warning("读不了 %s", path)
    return msgs


def _label(key):
    """会话键 → (类型, 展示名, 简写)。"""
    k = str(key or "")
    if k.startswith("private_"):
        return "私聊", "QQ %s" % k[8:], "QQ私聊"
    if k.startswith("group_"):
        rest = k[6:]
        if "_" in rest:
            gid, uid = rest.rsplit("_", 1)
            return "群聊", "群 %s · QQ %s" % (gid, uid), "QQ群聊"
        return "群聊", "群 %s" % rest, "QQ群聊"
    if k.startswith("web_"):
        who = k[4:]
        if who == "local":
            return "网页", "网页（未登录）", "网页"
        return "网页", "网页 · %s" % who, "网页"
    return "游客", "游客 %s" % k[:8], "游客"


def _sessions_from_files():
    """扫登录/网页的历史文件。

    **优先读归档**（`persona_archive_*.jsonl`）：那是只增不删的完整记录，
    而 `persona_*.jsonl` 只是给模型看的 12 条滑动窗口。
    2026-11-03 用户报「轮次有问题」，查下来就是窗口把早期对话挤掉了。
    """
    out = []
    seen = set()
    for p in glob.glob(os.path.join(SESS_DIR, "persona_archive_*.jsonl")):
        name = os.path.basename(p)
        key = name[len("persona_archive_"):-len(".jsonl")]
        msgs = _read_jsonl(p)
        if not msgs:
            continue
        seen.add(key)
        kind, label, short = _label(key)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0
        out.append({
            "key": key,
            "kind": kind,
            "short": short,
            "label": label,
            "source": "file",
            "messages": len(msgs),
            "turns": sum(1 for m in msgs if m.get("role") == "assistant"),
            "mtime": mtime,
            "path": p,
        })

    # 还没有归档的老会话：退回读窗口文件（至少能看到最近几轮）
    for p in glob.glob(os.path.join(SESS_DIR, "persona_*.jsonl")):
        name = os.path.basename(p)
        if name.startswith("persona_archive_"):
            continue
        key = name[len("persona_"):-len(".jsonl")]
        if key in seen:
            continue
        msgs = _read_jsonl(p)
        if not msgs:
            continue
        kind, label, short = _label(key)
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            mtime = 0
        out.append({
            "key": key,
            "kind": kind,
            "short": short,
            "label": label,
            "source": "file",
            "messages": len(msgs),
            "turns": sum(1 for m in msgs if m.get("role") == "assistant"),
            "mtime": mtime,
            "path": p,
            "truncated": True,        # 标记：这份只是窗口，不是全部
        })
    return out


def _sessions_from_guests():
    """扫游客目录，按 uuid 合并跨天的文件。"""
    out = []
    if not os.path.isdir(guest.GUEST_ROOT):
        return out
    seen = {}
    for day in sorted(os.listdir(guest.GUEST_ROOT)):
        d = os.path.join(guest.GUEST_ROOT, day)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".jsonl"):
                continue
            uid = f[: -len(".jsonl")]
            p = os.path.join(d, f)
            msgs = _read_jsonl(p)
            rec = seen.setdefault(uid, {"key": uid, "kind": "游客",
                                        "short": "游客", "label":
                                        "游客 %s" % uid[:8],
                                        "source": "guest",
                                        "messages": 0, "turns": 0,
                                        "mtime": 0, "path": p})
            rec["messages"] += len(msgs)
            rec["turns"] += sum(1 for m in msgs
                                if m.get("role") == "assistant")
            try:
                rec["mtime"] = max(rec["mtime"], os.path.getmtime(p))
            except OSError:
                pass
    out.extend(seen.values())
    return out


def _card_name(card_id):
    if not card_id:
        return ""
    c = persona.find_card(card_id)
    if not c:
        return card_id
    # 列表里的「演 XXX」优先显示自定义名字（苏暖），没有就用卡名。
    return persona.char_name(c, card_id)


@bp.route("/api/persona_admin/list")
def api_list():
    """所有玩过的会话，按最后活动时间倒序。"""
    rows = _sessions_from_files() + _sessions_from_guests()

    # 补上「当前演的是谁」——state 里有就显示，没有就算了
    st = persona._load_state()                     # noqa: SLF001
    for r in rows:
        ent = st.get(r["key"]) or {}
        if r["source"] == "guest":
            # 游客没有 base 记录，但有一条固定的线键，名字挂在线上（2026-11-09）
            from app.persona_api import GUEST_CARD_ID
            gkey = persona.card_key("guest_%s" % r["key"], GUEST_CARD_ID)
            nm = str((st.get(gkey) or {}).get("char_name") or "").strip()
            r["card"] = nm or _card_name(GUEST_CARD_ID)
        else:
            # 存档级名字（2026-11-09）：玩家在网页右栏改过名就优先显示它，
            # 没改过才回落到卡上的默认名。
            nm = str(ent.get("char_name") or "").strip()
            r["card"] = nm or _card_name(ent.get("card"))
        r["state"] = str(ent.get("state") or "")

    rows.sort(key=lambda r: r["mtime"], reverse=True)
    for r in rows:
        r["mtime_text"] = _ts(r["mtime"])
        r.pop("path", None)
    return jsonify({"ok": True, "total": len(rows), "sessions": rows})


def _ts(sec):
    if not sec:
        return ""
    import time
    return time.strftime("%m-%d %H:%M", time.localtime(sec))

@bp.route("/api/persona_admin/detail")
def api_detail():
    """某个会话的完整对话。游客会跨天合并。"""
    key = str(request.args.get("key") or "").strip()
    source = str(request.args.get("source") or "file").strip()
    if not key:
        return jsonify({"ok": False, "error": "缺 key"}), 400

    if source == "guest":
        uid = guest.check_id(key) or key
        raw = guest.load_hist(uid)
        path = ""
    else:
        # 键里可能有路径分隔符，挡一下
        if "/" in key or "\\" in key or ".." in key:
            return jsonify({"ok": False, "error": "key 不合法"}), 400
        # 优先读归档（完整记录）；没有就退回窗口文件（只有最近 12 条）
        arc = os.path.join(SESS_DIR, "persona_archive_%s.jsonl" % key)
        win = os.path.join(SESS_DIR, "persona_%s.jsonl" % key)
        if os.path.isfile(arc):
            path = arc
        elif os.path.isfile(win):
            path = win
        else:
            return jsonify({"ok": False, "error": "这个会话没有记录"}), 404
        raw = _read_jsonl(path)

    kind, label, short = _label(key)
    st = persona._load_state().get(key) or {}       # noqa: SLF001
    mt = 0
    if path:
        try:
            mt = os.path.getmtime(path)
        except OSError:
            pass
    return jsonify({
        "ok": True,
        "key": key,
        "kind": kind,
        "short": short,
        "label": label,
        "card": (str(st.get("char_name") or "").strip()
                 or _card_name(st.get("card"))),
        "note": str(st.get("note") or ""),
        "state": str(st.get("state") or ""),
        "messages": _norm(raw),
        "mtime": mt,
        "mtime_text": _ts(mt),
        "total": len(raw),
    })


@bp.route("/api/persona_admin/delete", methods=["POST"])
def api_delete():
    """删掉一个会话（游客目录里的文件 / 登录历史 + state）。"""
    body = request.get_json(silent=True) or {}
    key = str(body.get("key") or "").strip()
    source = str(body.get("source") or "file").strip()
    if not key:
        return jsonify({"ok": False, "error": "缺 key"}), 400

    if source == "guest":
        uid = guest.check_id(key) or key
        n = 0
        if os.path.isdir(guest.GUEST_ROOT):
            for day in os.listdir(guest.GUEST_ROOT):
                p = os.path.join(guest.GUEST_ROOT, day, uid + ".jsonl")
                if os.path.isfile(p):
                    os.remove(p)
                    n += 1
        # 游客的向量库挂在 `guest_<uid>__<卡>` 上，一起清掉（2026-11-05）
        try:
            st = persona._load_state()                  # noqa: SLF001
            for k in list(st):
                if k.startswith("guest_%s__" % uid):
                    persona_rag.drop_line(k)
        except Exception:                                # noqa: BLE001
            log.exception("游客向量库清理失败 %s", uid)
        return jsonify({"ok": True, "deleted": n})

    if "/" in key or "\\" in key or ".." in key:
        return jsonify({"ok": False, "error": "key 不合法"}), 400
    p = os.path.join(SESS_DIR, "persona_%s.jsonl" % key)
    if os.path.isfile(p):
        os.remove(p)
    # state 里那条也清掉，不然列表还挂着「扮演中」
    st = persona._load_state()                      # noqa: SLF001
    if key in st:
        st.pop(key)
        persona._save_state(st, deleted={key})      # noqa: SLF001
    # 向量库也只删这一条线（2026-11-05）
    persona_rag.drop_line(key)
    return jsonify({"ok": True, "deleted": 1})


# ─── 角色扮演管理：默认背景 / 角色背景（2026-11-05）────────────
#
# 页面 `/characters`（5174）用。5174 本身不对外，但如果以后有人往 5175
# 转发，仍按卡管理的口径拦一道：本地直连放行，经网关只给站主。

def _may_manage():
    from app.card_api import _may_write
    return _may_write()


def _manage_denied(reason):
    return jsonify({"ok": False, "error": reason}), 403


@bp.route("/api/persona_admin/background/default")
def admin_default_background():
    """页面加载时看当前全局默认图；同时带出内置兜底图，方便显示「当前用哪张」。"""
    return jsonify({
        "ok": True,
        "default": chat_bg.info("default", ""),
        "builtin": chat_bg.info("builtin", ""),
    })


@bp.route("/api/persona_admin/background/default", methods=["POST"])
def admin_upload_default_background():
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    try:
        info = chat_bg.save(request.files.get("file"), "default", "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "background": info})


@bp.route("/api/persona_admin/background/default", methods=["DELETE"])
def admin_delete_default_background():
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    chat_bg.delete("default", "")
    return jsonify({"ok": True, "default": chat_bg.info("default", ""),
                    "builtin": chat_bg.info("builtin", "")})


@bp.route("/api/persona_admin/background/card/<cid>", methods=["POST"])
def admin_upload_card_background(cid):
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    if persona.get_card(cid) is None:
        return jsonify({"ok": False, "error": "没有这张卡"}), 404
    try:
        info = chat_bg.save(request.files.get("file"), "card", cid)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "background": info})


@bp.route("/api/persona_admin/background/card/<cid>", methods=["DELETE"])
def admin_delete_card_background(cid):
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    chat_bg.delete("card", cid)
    return jsonify({"ok": True, "background": chat_bg.info("card", cid)})

# ─── 游客会话查看：管理员 QQ 名单（2026-11-08）────────────
#
# 名单本身存在 `state/guest_admins.json`，读写逻辑在 `app/guest_view.py`
# （5175 那边也要读同一份，放这里会让两个进程互相依赖）。

@bp.route("/api/persona_admin/admins")
def api_admins():
    from app import guest_view
    return jsonify({"ok": True, "ids": guest_view.list_admins()})


@bp.route("/api/persona_admin/admins", methods=["POST"])
def api_add_admin():
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    from app import guest_view
    data = request.get_json(silent=True) or {}
    qq = str(data.get("qq") or "").strip()
    ids, err = guest_view.add_admin(qq)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "ids": ids})


@bp.route("/api/persona_admin/admins/<qq>", methods=["DELETE"])
def api_del_admin(qq):
    ok, why = _may_manage()
    if not ok:
        return _manage_denied(why)
    from app import guest_view
    return jsonify({"ok": True, "ids": guest_view.remove_admin(qq)})
