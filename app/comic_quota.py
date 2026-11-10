"""群友漫画台：身份判据、作品归属、按账号配额与模型白名单。

## 为什么独立一层

5174 的漫画工作台（`app/comic_api.py`）本来是站主自用的，完全不认账号。
开放给群友后要加四件事：谁在用、哪些作品是他的、还能用几次、能用哪个模型。
这些都属于**准入与账本**，不是渲染逻辑，单独放一层，`comic_api` 只做挂钩，
不让它的每个端点都塞一遍身份判断。

## 身份从哪来（关键安全边界）

5175（`friend_app.py`）的 `_forward()` 会做两件事：带 `X-Friend-Token` 内部令牌、
把登录 Cookie 解出来的 QQ 号**写死**塞进 `qq` 参数/表单（不信客户端传值）。
所以这里只认「令牌对 + 有 qq」这一种组合，二者缺一就是站主直连 5174，
**全部放行、一个限制都不加** —— 站主版 `/comic` 的行为必须原样保留。

前端怎么显示、有没有藏按钮，都不影响这一层：直连 5174 绕过 5175 也拿不到
别人的作品（owner 过滤）和额外次数（配额），因为判据是服务端令牌，不是 URL。

## 账本

`state/comic_friend.json`（state/ 不进仓库）。缺省就是零配置：默认额度 + 空
白名单（= 走站主全局默认模型）。管理页改额度、加次数、配模型都写这里。
"""

import copy
import json
import os
import threading
import time

from flask import request

from app.config import BASE_DIR

_FILE = os.path.join(BASE_DIR, "state", "comic_friend.json")
_LOCK = threading.RLock()

# 免费默认。写死在代码里是刻意的：state 文件丢失/损坏时也能开服，不会把
# 群友全挡在门外，管理页随后补档即可。
DEFAULTS = {
    "script": 1,       # 剧本生成（站主模型）
    "revise": 3,       # 修订助手，只在 changed=true 落地时扣
    "comic": 1,        # 漫画「一次生成机会」= 一本
    "portrait": 2,     # 写真「一次」= 一本 5 张
    "max_panels": 10,  # 一本最多几格
    "max_shots": 5,    # 写真一本最多几张
}
KINDS = ("script", "revise", "comic", "portrait")
# 管理页可调的档位（格数）。改额度直接改数字也认，这里只是前端快捷选项。
PANEL_TIERS = (10, 15, 20, 25, 30)

_EMPTY = {"defaults": dict(DEFAULTS), "models": [], "accounts": {},
          "charged": {}, "deliveries": []}


def _read():
    """读账本；不存在/坏了都退回空账本，绝不因为账本故障挡住整条链路。"""
    with _LOCK:
        try:
            with open(_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("账本不是对象")
        except (OSError, ValueError):
            return copy.deepcopy(_EMPTY)
        out = copy.deepcopy(_EMPTY)
        for k in _EMPTY:
            if k in data and isinstance(data[k], type(_EMPTY[k])):
                out[k] = data[k]
        return out


def _write(data):
    with _LOCK:
        os.makedirs(os.path.dirname(_FILE), exist_ok=True)
        tmp = _FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, _FILE)


def _touch(acc):
    """记一次「这个账号动过漫画台」。管理页靠它回答「谁用过、最近谁在用」。

    只在真的扣/退/记账时写，不挂在每次 GET 上——否则光是页面轮询就会把
    「最近使用」刷成当前时间，那一列就没意义了。
    """
    acc["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")


# ─── 身份 ──────────────────────────────────────────────

def friend_qq():
    """当前请求是不是 5175 转发来的群友请求 → QQ 号；站主直连返回 ""。

    令牌在 `app/friend_auth.secret()`，只有 5174/5175 两个进程知道。客户端
    自己伪造 `qq` 参数没用：没有令牌这里直接返回空串，而空串意味着「站主」，
    站主路径不查 owner、不查配额 —— 但 5174 只监听本机/内网，群友碰不到。
    """
    try:
        from app import friend_auth
        good = friend_auth.secret()
    except Exception:
        return ""
    got = str(request.headers.get("X-Friend-Token") or "")
    if not got or got != good:
        return ""
    qq = str(request.args.get("qq") or "").strip()
    if not qq:
        try:
            qq = str((request.get_json(silent=True) or {}).get("qq") or "").strip()
        except Exception:
            qq = ""
    return qq if qq.isdigit() else ""


def is_friend_task(task_dir):
    """这个任务目录是不是群友的（判据是目录里的 owner.json，不看当前请求）。

    渲染线程没有 request 上下文，落盘目录与「跳过 2x 放大」都要在线程里判断，
    所以归属必须落盘，不能只靠请求对象。
    """
    try:
        return os.path.isfile(os.path.join(str(task_dir or ""), "owner.json"))
    except OSError:
        return False


def owner_of(task_dir):
    """→ QQ 号；站主任务返回 ""。"""
    try:
        with open(os.path.join(str(task_dir or ""), "owner.json"),
                  encoding="utf-8") as f:
            return str((json.load(f) or {}).get("qq") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def set_owner(task_dir, qq):
    """新建任务时钉归属。只写一次：已存在就不覆盖，防止接上一集时误改主人。"""
    fp = os.path.join(str(task_dir), "owner.json")
    if os.path.isfile(fp):
        return owner_of(task_dir)
    os.makedirs(str(task_dir), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump({"qq": str(qq)}, f, ensure_ascii=False)
    return str(qq)


def can_read(task_dir, qq):
    """群友能不能读这个任务。站主（qq 空）一律放行。"""
    if not qq:
        return True
    return owner_of(task_dir) == qq


def output_base(task_dir):
    """群友任务在 ComfyUI `output/` 下的目录体：`群友漫画台/<QQ>/<作品>`。

    返回空串 = 站主任务，沿用原来的 `web/漫画_<任务号>`。`comfy_out.body_for`
    会再拼一层渠道名，所以这里**只能给三段**：`_join` 的 `_MAX_DEPTH=4`
    会把更深的层级截掉，渠道那一段必须留给它。

    目录名过一遍 `comfy_out._join`：任务号可能带中文标题，标题里出现 `:` `?`
    这类 Windows 非法字符要先洗掉，否则 ComfyUI 写盘直接失败。
    """
    qq = owner_of(task_dir)
    if not qq:
        return ""
    from app import comfy_out
    name = os.path.basename(str(task_dir).rstrip("\\/")) or "作品"
    return comfy_out._join("群友漫画台", qq, name)


# ─── 模型白名单 ────────────────────────────────────────

def models():
    """给群友选的编剧模型。空 = 只给「站主默认」一项，不暴露所有中转站别名。"""
    data = _read()
    out = []
    for m in data["models"]:
        if not isinstance(m, dict):
            continue
        p, v = str(m.get("provider") or "").strip(), str(m.get("model") or "").strip()
        if not p:
            continue
        out.append({"provider": p, "model": v,
                    "label": str(m.get("label") or v or p).strip()})
    return out


def resolve_model(provider, model):
    """群友请求的 provider/model 收敛到白名单 → (provider, model)。

    不在白名单就退回站主全局默认（两个空串，下游 `llm` 自己按默认走）。
    不报错：白名单刚被站主改小、前端还缓存着旧下拉时，静默退回比 400 友好。
    """
    p, v = str(provider or "").strip(), str(model or "").strip()
    for m in models():
        if m["provider"] == p and (not m["model"] or m["model"] == v):
            return m["provider"], m["model"]
    return "", ""


# ─── 配额 ──────────────────────────────────────────────

def limits(qq):
    """这个账号的上限 = 全局默认 + 按账号覆盖。"""
    data = _read()
    lim = dict(DEFAULTS)
    lim.update(data["defaults"] or {})
    acc = (data["accounts"].get(str(qq)) or {})
    lim.update(acc.get("limits") or {})
    for k in KINDS:
        lim[k] = max(0, int(lim.get(k) or 0))
    lim["max_panels"] = max(1, int(lim.get("max_panels") or 10))
    lim["max_shots"] = max(1, int(lim.get("max_shots") or 5))
    return lim


def usage(qq):
    """这个账号各额度已用多少。"""
    data = _read()
    used = (data["accounts"].get(str(qq)) or {}).get("used") or {}
    return {k: max(0, int(used.get(k) or 0)) for k in KINDS}


def remaining(qq, kind):
    return max(0, limits(qq).get(kind, 0) - usage(qq).get(kind, 0))


def snapshot(qq):
    """给页面顶部显示的一行：还剩几次、一本几格。"""
    lim, used = limits(qq), usage(qq)
    return {"left": {k: max(0, lim[k] - used[k]) for k in KINDS},
            "used": used, "limits": lim, "qq": str(qq)}


def consume(qq, kind, n=1):
    """扣次数 → bool。额度不足不扣、返回 False。原子（RLock + 读改写）。"""
    if kind not in KINDS:
        return False
    with _LOCK:
        data = _read()
        acc = data["accounts"].setdefault(str(qq), {})
        used = acc.setdefault("used", {})
        lim = dict(DEFAULTS)
        lim.update(data["defaults"] or {})
        lim.update(acc.get("limits") or {})
        cur = int(used.get(kind) or 0)
        if cur + n > int(lim.get(kind) or 0):
            return False
        used[kind] = cur + n
        _touch(acc)
        _write(data)
        return True


def refund(qq, kind, n=1):
    """后台任务失败时把预扣的次数退回来（不会退成负数）。"""
    if kind not in KINDS:
        return
    with _LOCK:
        data = _read()
        acc = data["accounts"].setdefault(str(qq), {})
        used = acc.setdefault("used", {})
        used[kind] = max(0, int(used.get(kind) or 0) - n)
        _touch(acc)
        _write(data)


def charged(qq, kind, task_id):
    """「这一本有没有扣过次数」——重跑/续跑不能重复扣。"""
    return str(task_id) in ((_read()["charged"].get(str(qq)) or {}).get(kind) or [])


def mark_charged(qq, kind, task_id):
    with _LOCK:
        data = _read()
        slot = data["charged"].setdefault(str(qq), {})
        ids = slot.setdefault(kind, [])
        if str(task_id) not in ids:
            ids.append(str(task_id))
            del ids[:-500]          # 只留最近 500 本，账本不无限长
            _touch(data["accounts"].setdefault(str(qq), {}))
            _write(data)


# ─── 管理页 ────────────────────────────────────────────

def admin_view():
    """管理页要看到的全部数据（不含任何密钥）。

    主体是「谁用了漫画台」：一行一个 QQ，给全各项剩余/已用/上限、每本格数、
    最近一次动它的时间、跑了几本。`accounts` 是**有人真用过**才建号——站主要
    给还没用过的人先加次数，走 `admin_update({"add": ...})`，它会自动建号。
    """
    data = _read()
    out = {"defaults": dict(DEFAULTS), "models": models(), "accounts": [],
           "deliveries": list(data["deliveries"] or [])[-20:],
           "panel_tiers": list(PANEL_TIERS)}
    out["defaults"].update(data["defaults"] or {})
    for qq, acc in sorted(data["accounts"].items()):
        lim, used = limits(qq), usage(qq)
        ch = data["charged"].get(qq) or {}
        works = set(ch.get("comic") or []) | set(ch.get("portrait") or [])
        out["accounts"].append({
            "qq": qq, "limits": lim, "used": used,
            "left": {k: max(0, lim[k] - used[k]) for k in KINDS},
            "last_at": str(acc.get("last_at") or ""),
            "works": len(works), "note": str(acc.get("note") or "")})
    return out


def admin_update(payload):
    """管理页改配置。只认白名单字段，其余忽略；返回更新后的 admin_view()。

    `add: {qq, kind, n}` = 给这个账号上限加 n 次（原子、自动建号）。
    `account: {qq, limits, used, note}` = 直接设整行，给改格数档位 / 备注用。
    """
    p = payload or {}
    with _LOCK:
        data = _read()
        if isinstance(p.get("defaults"), dict):
            for k in list(DEFAULTS):
                if k in p["defaults"]:
                    try:
                        data["defaults"][k] = max(0, int(p["defaults"][k]))
                    except (TypeError, ValueError):
                        pass
        if isinstance(p.get("models"), list):
            data["models"] = [
                {"provider": str(m.get("provider") or "").strip()[:80],
                 "model": str(m.get("model") or "").strip()[:120],
                 "label": str(m.get("label") or "").strip()[:80]}
                for m in p["models"] if isinstance(m, dict)
                and str(m.get("provider") or "").strip()][:20]
        # 「加次数」是增量、在服务端算，不让前端把整行上限读回去再提交：
        # 两个页面同时点 +1 时，前端算好的旧值会互相覆盖。账号不存在就自动建，
        # 站主可以给还没用过漫画台的人先加。
        add = p.get("add")
        if (isinstance(add, dict) and str(add.get("qq") or "").isdigit()
                and str(add.get("kind") or "") in KINDS):
            qq, kind = str(add["qq"]), str(add["kind"])
            try:
                n = max(0, min(99, int(add.get("n") or 1)))
            except (TypeError, ValueError):
                n = 1
            slot = data["accounts"].setdefault(qq, {})
            lim = slot.setdefault("limits", {})
            # 账号没单独设过这一项时，要从「当前生效额度」起加，不能从 0 起算
            # （否则给新人 +1 剧本会变成总共只有 1 次）。只冻结被加的这一项，
            # 其它额度继续跟随全局默认。
            base = dict(DEFAULTS)
            base.update(data["defaults"] or {})
            base.update(lim)
            lim[kind] = max(0, int(base.get(kind) or 0) + n)
            # 站主加次数不算「这个人用过漫画台」：最近使用只由他自己真的
            # 扣/退/记账刷新，否则每加一次都会把那一列刷成现在的时间。
        acc = p.get("account")
        if isinstance(acc, dict) and str(acc.get("qq") or "").isdigit():
            qq = str(acc["qq"])
            slot = data["accounts"].setdefault(qq, {})
            if isinstance(acc.get("limits"), dict):
                lim = slot.setdefault("limits", {})
                for k in KINDS:
                    if k in acc["limits"]:
                        try:
                            lim[k] = max(0, int(acc["limits"][k]))
                        except (TypeError, ValueError):
                            pass
                for k in ("max_panels", "max_shots"):
                    if k in acc["limits"]:
                        try:
                            lim[k] = max(1, int(acc["limits"][k]))
                        except (TypeError, ValueError):
                            pass
            if isinstance(acc.get("used"), dict):
                used = slot.setdefault("used", {})
                for k in KINDS:
                    if k in acc["used"]:
                        try:
                            used[k] = max(0, int(acc["used"][k]))
                        except (TypeError, ValueError):
                            pass
            if "note" in acc:
                slot["note"] = str(acc["note"] or "")[:200]
        _write(data)
    return admin_view()


def note_delivery(qq, task_id, state, detail=""):
    """QQ 直发结果记一笔，管理页能看失败记录并手动重发。"""
    import time
    with _LOCK:
        data = _read()
        rows = data.setdefault("deliveries", [])
        rows.append({"qq": str(qq), "task": str(task_id), "state": str(state),
                     "detail": str(detail or "")[:300],
                     "at": time.strftime("%Y-%m-%d %H:%M:%S")})
        del rows[:-200]
        _write(data)
