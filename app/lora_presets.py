# -*- coding: utf-8 -*-
"""群友「自定义预设」（`zdy-` 前缀）的唯一事实源。

一条预设 = 一组 **LoRA 槽位组合**（哪个槽 / 开没开 / 强度多少），
**不含提示词**——提示词每次现写。落 `state/lora_presets.json`，按 QQ 隔离：

    {"2509355624": {"光影": {"loras": [...], "public": true, "ts": 1.7e9}}}

规则（2026-10-30 用户拍板）：

- 前缀固定 `zdy-`，**所有人共用**；`-` 后面才是各人自己的命名
- 每人最多 **1 个公开**、**5 个私有**（公开的那个不占私有额度）
- **公开名全站唯一**；私有名只在自己范围内唯一，撞别人的公开名没关系
- **私有预设只在私聊可用**，群里一律只能调公开池
- 想换公开的那个不用先取消，**新的顶替旧的**，但要告诉用户原来那个被取消了

前缀容错：全角 `－`、破折号 `—` / `–`、连字符 `‐` / `‑`、减号 `−` 都当 `-`；
`zdy` / `Zdy` / `ZDY` 大小写都认。中文输入法把 `-` 打成全角是最高频的
失败原因——不做这个容错，群友会觉得「明明说对了却用不了」。
"""

import json
import logging
import os
import re
import threading
import time

from app.config import state_path

log = logging.getLogger("lora_presets")

PREFIX = "zdy-"
MAX_PUBLIC = 1
MAX_PRIVATE = 5

# 名字：1~16 个汉字 / 字母 / 数字 / 下划线。
# **不许空格**——空格是 QQ 消息里的断词边界，允许了「zdy-白 天」会被截成「白」。
NAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fa5]{1,16}$")

# `-` 的各种写法，放进字符类（放开头是字面量，不做范围）
_SEP = "[\u002d\uff0d\u2014\u2013\u2010\u2011\u2212]"

# 一句话里找令牌用 search（群友会说「帮我用 zdy-光影 画个…」，令牌常在句子中间）。
# 左边界 `(?<![0-9A-Za-z_])`：`azdy-光影` / `my_zdy-x` 这种是别人的单词，
# 不该被当成令牌截走。
TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z_])zdy\s*" + _SEP + r"\s*([0-9A-Za-z_\u4e00-\u9fa5]{1,16})",
    re.I)

_LOCK = threading.Lock()


def _path():
    # 不缓存：state_path 在测试进程里会切到临时目录，模块级缓存会串味
    return state_path("lora_presets.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    try:
        p = _path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except OSError:
        log.warning("预设写不进去", exc_info=True)
        return False


# ─── 名字 ────────────────────────────────────────────────

def extract(text):
    """从一句话里找出 `zdy-xxx` 令牌，返回 (name, start, end)；没有返回 None。

    只取**第一个**：一句话里出现两个令牌本身就是有歧义的，猜错了浪费的是
    一整个出图等待，不如让调用方明确报错。
    """
    m = TOKEN_RE.search(text or "")
    if not m:
        return None
    return m.group(1), m.start(), m.end()


def clean_name(raw):
    """把用户输入洗成干净的名字（网页表单和 QQ 令牌共用）。

    允许直接粘 `zdy-光影`——把前缀剥掉。不剥的话会出现「存进去叫 `zdy-光影`、
    叫的时候变成 `zdy-zdy-光影`」这种越用越长的循环。
    """
    s = str(raw or "").strip()
    m = re.match(r"zdy\s*" + _SEP + r"\s*(.+)$", s, re.I)
    if m:
        s = m.group(1).strip()
    return s


def check_name(name):
    """名字合法返回 ""，不合法返回给人看的一句话。"""
    if not name:
        return "预设名不能为空"
    if not NAME_RE.match(name):
        return "预设名只能是 1~16 个汉字、字母、数字或下划线（不能有空格）"
    return ""


def label(name):
    """对外展示的完整令牌（`zdy-光影`）。"""
    return PREFIX + name


# ─── 查询 ────────────────────────────────────────────────

def for_qq(qq):
    """某人自己的全部预设 {name: {...}}（公开的也在里面）。"""
    return dict(load().get(str(qq)) or {})


def quota(qq):
    """额度用量，给网页顶部显示「公开 0/1 · 私有 3/5」。"""
    row = load().get(str(qq)) or {}
    pub = sum(1 for p in row.values() if p.get("public"))
    priv = sum(1 for p in row.values() if not p.get("public"))
    return {"public": pub, "public_max": MAX_PUBLIC,
            "private": priv, "private_max": MAX_PRIVATE}


def public_owner(name, data=None):
    """公开池里这个名字归谁；没人公开返回 ""。"""
    for qq, row in (data if data is not None else load()).items():
        p = row.get(name)
        if p and p.get("public"):
            return str(qq)
    return ""


def public_pool():
    """公开池 [(name, qq)]，按名字排序。"""
    out = []
    for qq, row in load().items():
        for name, p in row.items():
            if p and p.get("public"):
                out.append((name, str(qq)))
    out.sort()
    return out


def resolve(raw_name, qq=None, in_group=False):
    """把令牌落成一条真实预设。返回 (preset, err)。

    **私聊优先自己的**：同名的私有和别人的公开撞车时，他私聊说这个词要的是
    自己刚调的那套，不是别人的（这也是「允许同名、按场景分流」的另一半）。
    群聊只有公开池一条路。
    """
    name = clean_name(raw_name)
    bad = check_name(name)
    if bad:
        return None, bad

    data = load()
    if in_group:
        for owner, row in data.items():
            p = row.get(name)
            if p and p.get("public"):
                return dict(p, _owner=owner), ""
        if qq and name in (data.get(str(qq)) or {}):
            return None, ("「%s」是你的私有预设，群里用不了——私聊我就能用，"
                          "或者发 /自定义预设 看公开的" % label(name))
        return None, ("没有「%s」这个公开预设，发 /自定义预设 看现在能用哪些"
                      % label(name))

    mine = data.get(str(qq)) or {}
    if name in mine:
        return dict(mine[name], _owner=str(qq)), ""
    for owner, row in data.items():
        p = row.get(name)
        if p and p.get("public"):
            return dict(p, _owner=owner), ""

    hint = []
    if mine:
        hint.append("你自己存的：" + "、".join(label(n) for n in list(mine)[:6]))
    pool = public_pool()
    if pool:
        hint.append("公开的：" + "、".join(label(n) for n, _ in pool[:8]))
    msg = "没有「%s」这个预设" % label(name)
    return None, msg + ("。\n" + "；".join(hint) if hint else "")


def describe_pool(nick=None, limit=60):
    """`/自定义预设` 的正文。纯代码生成，不过模型——省钱也不会写错名字。"""
    pool = public_pool()
    if not pool:
        return ("现在还没有人公开自定义预设。\n"
                "群友在网页上调好 LoRA 组合、存为预设并设为公开，这里就能看到。")
    lines = ["当前可用的公开自定义预设（%d 个）：" % len(pool)]
    for name, qq in pool[:limit]:
        who = ""
        try:
            who = (nick(qq) if nick else "") or ""
        except Exception:
            who = ""
        lines.append("  " + label(name) + ("（%s）" % who if who else ""))
    if len(pool) > limit:
        lines.append("  ……还有 %d 个" % (len(pool) - limit))
    lines.append("用法：%s名字 + 提示词，例：%s光影，一个女孩站在窗边"
                 % (PREFIX, PREFIX))
    return "\n".join(lines)


# ─── 增删改 ──────────────────────────────────────────────

def save(qq, raw_name, loras, public=False, skill=""):
    """新建 / 更新一条预设。返回 (ok, 给人看的话)。

    `skill` 记的是**这套槽位是在哪个渠道上数的**。必须存：槽位编号和 LoRA
    文件名都绑死在那个渠道的模板上，套到别的渠道轻则强度错位、重则直接
    「这些 lora 不存在」。存了它，没点名渠道时就能自动回到这个渠道。

    public=True 时：公开名全站唯一（撞了就拒）+ 每人只留一个（旧的自动降级）。
    「自动顶替」是 2026-10-30 用户拍板的，但**必须告诉他哪个被顶掉了**——
    不然他会以为两个都是公开的。
    """
    qq = str(qq)
    name = clean_name(raw_name)
    bad = check_name(name)
    if bad:
        return False, bad
    rows = list(loras or [])
    if not [s for s in rows if s.get("on", True)]:
        return False, "一个 LoRA 都没开，存了也没用"

    with _LOCK:
        data = load()
        row = data.setdefault(qq, {})
        kicked = ""
        if public:
            owner = public_owner(name, data)
            if owner and owner != qq:
                return False, "「%s」这个名字已经被别人公开了，换一个" % name
            for other, p in row.items():
                if other != name and p.get("public"):
                    p["public"] = False
                    kicked = other
                    break
        else:
            priv = [n for n, p in row.items() if not p.get("public") and n != name]
            if len(priv) >= MAX_PRIVATE:
                return False, ("私有预设已经有 %d 个（上限 %d 个），"
                               "先删一个再存" % (len(priv), MAX_PRIVATE))
        row[name] = {
            # **连关着的槽一起存**：模板里默认开着的槽（比如 8 号 nai5），
            # 群友在网页上把它关了，只存「开着的」就把这个动作丢了——套用时
            # 模板那个槽还开着，出来的风格和他看到的不是一回事。
            "loras": [{"slot": s.get("slot"), "on": bool(s.get("on", True)),
                       "strength": s.get("strength"),
                       "name": s.get("name") or ""} for s in rows],
            "public": bool(public),
            "skill": str(skill or ""),
            "ts": time.time(),
        }
        if not _write(data):
            return False, "保存失败"

    msg = ("已公开「%s」" if public else "已保存「%s」") % name
    if kicked:
        msg += "，原来的公开预设「%s」已自动取消公开" % kicked
    return True, msg


def set_public(qq, raw_name, public):
    """只改公开状态（网页上那个开关用）。返回 (ok, 话)。"""
    qq = str(qq)
    name = clean_name(raw_name)
    kicked = ""
    with _LOCK:
        data = load()
        row = data.get(qq) or {}
        p = row.get(name)
        if not p:
            return False, "没有「%s」这个预设" % name
        if public:
            if p.get("public"):
                return True, "「%s」已经是公开的了" % name
            owner = public_owner(name, data)
            if owner and owner != qq:
                return False, ("「%s」这个名字已经被别人公开了，"
                               "改个名再公开" % name)
            for other, q in row.items():
                if other != name and q.get("public"):
                    q["public"] = False
                    kicked = other
                    break
            p["public"] = True
        else:
            p["public"] = False
        if not _write(data):
            return False, "保存失败"

    if not public:
        return True, "「%s」已取消公开" % name
    return True, ("已公开「%s」" % name
                  + ("，原来的「%s」已自动取消公开" % kicked if kicked else ""))


def delete(qq, raw_name):
    """删掉一条。返回是否真删了。"""
    qq = str(qq)
    name = clean_name(raw_name)
    with _LOCK:
        data = load()
        row = data.get(qq) or {}
        if name not in row:
            return False
        del row[name]
        if not row:
            data.pop(qq, None)
        _write(data)
    return True


# ─── 给生图工具用 ────────────────────────────────────────

def lora_string(preset):
    """把槽位摊成 `名字:强度` 逗号串（喂给 generate_image 现成的 lora 通道）。

    只取开着的槽、强度 > 0 的——强度归零等于没开，带进去只会把别人的槽顶掉。
    """
    bits = []
    for s in (preset or {}).get("loras") or []:
        if not s.get("on", True):
            continue
        n = (s.get("name") or "").strip()
        if not n:
            continue
        try:
            v = float(s.get("strength"))
        except (TypeError, ValueError):
            v = 1.0
        if v <= 0:
            continue
        bits.append("%s:%.2f" % (n, v))
    return ",".join(bits)
