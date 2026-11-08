# -*- coding: utf-8 -*-
r"""v2 成人向状态条的**真实轮次**测试台（2026-11-08）。

和 `tests/test_affection_v2_adult.py` 的分工：
    · 那边是单元测试——直接喂 delta 给 `apply()`，验证 clamp/闸门/回落。
    · 这边是**跑真模型**：给一条副本线设好数值，喂一段对话，看
      「编剧判了什么 → 结算成什么 → 演员写出了什么」三样对不对得上。

用法（都在 `D:\AI\agent_my_test` 下）：

    # 1. 看有哪些场景（不花钱）
    python tools/adult_replay.py scenarios

    # 2. 跑一个场景（会用真 LLM，会写 state）
    python tools/adult_replay.py run gating

    # 3. 跑完看这一轮的完整记录
    python tools/adult_replay.py show rp_adult_gating

    # 4. 清掉副本（只认 rp_adult_ 前缀，碰不到真人线）
    python tools/adult_replay.py drop rp_adult_gating
    python tools/adult_replay.py drop-all

设计纪律（照项目既有约定）：
    · **一律跑副本**：key 以 `rp_adult_` 开头，母本只读不写。
    · **只读母本**：不引用任何真人线的聊天原文，用的是场景里写死的台词。
    · **不重启任何进程**：这脚本自己就是一个 python 进程。
    · 每轮把「编剧 delta / 结算前后 / 注入块 / 演员正文」全落盘到
      `state/adult_replay/<key>.jsonl`，事后可以逐轮核对。
"""

import argparse
import copy
import io
import json
import os
import re
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

COPY_PREFIX = "rp_adult_"
SAFE_KEY = re.compile(r"^[A-Za-z0-9_\-]+$")
REPLAY_DIR = os.path.join(BASE, "state", "adult_replay")
CARDS_PATH = os.path.join(BASE, "agents", "qq", "characters.json")


# ─── 场景表 ──────────────────────────────────────────
#
# 每条场景 = 一个「起始数值 + 几句台词」。跑完拿结果对预期。
# 台词是**通用日常场景**，不写露骨内容；露骨的部分由模型自己演进，
# 我们只验证数值和闸门对不对。
#
SCENARIOS = {
    "gating": {
        "desc": "开发度闸门：一处开发很低时，冲动最多到「想要」",
        "card": "sunna",
        "start": {
            "value": 20,          # 好感（正向）
            "dev": {"pussy": 5},
            "shame": 25,
        },
        "lines": [
            "我坐到你旁边，手搭在你肩上。",
            "（我把你搂过来，额头抵着你的）",
        ],
        "expect": [
            "arousal 不高于 want（dev 均值 < 40）",
            "corrupt / submit 恒 0（好感为正）",
        ],
    },
    "warmup": {
        "desc": "正常推进：前戏把敏感度推上去，单纯插入只推开发度",
        "card": "sunna",
        "start": {
            "value": 60,
            "dev": {"mouth": 20, "nipple": 30, "pussy": 40, "anus": 0, "skin": 25},
            "sense": {"mouth": 20, "nipple": 35, "pussy": 45, "anus": 0, "skin": 25},
            "shame": 40,
            "excite": 30,
        },
        "lines": [
            "我把你抱到腿上，慢慢吻你。",
            "（手顺着你的腰往下）",
            "（继续，没有停）",
        ],
        "expect": [
            "sense 涨幅应该明显（前戏）",
            "dev 也会涨，但涨幅小一些",
            "excite 单调上升，到顶后回落 50",
        ],
    },
    "shame_cap": {
        "desc": "羞耻软顶：正常亲密推不动 50 以上，露出类事件能",
        "card": "sunna",
        "start": {
            "value": 70,
            "dev": {p: 50 for p in ("mouth", "nipple", "pussy", "anus", "skin")},
            "sense": {p: 55 for p in ("mouth", "nipple", "pussy", "anus", "skin")},
            "shame": 48,
            "excite": 40,
        },
        "lines": [
            "我们在卧室里，我吻你。",
            "（继续，房间里只有我们）",
        ],
        "expect": [
            "shame 最高冲到 50 就停（普通场景）",
            "第二次同类事件涨幅更小",
        ],
    },
    "negative": {
        "desc": "负好感：堕落/屈服才能涨",
        "card": "sunna",
        "start": {
            "value": -25,
            "dev": {"pussy": 30},
            "shame": 60,
            "corrupt": 0,
            "submit": 0,
        },
        "lines": [
            # 台词里必须带上「她的反应」，否则编剧判不出 corrupt——
            # 那条轴的定义是「她不愿意 + 身体有反应」，缺一半就永远 0。
            "我把你按在墙上，不许你躲。她挣了两下没挣开，胸口贴着我的，喘得很快。",
            "「叫主人。」她咬着嘴唇不吭声，耳朵却红了，腿有点软。",
        ],
        "expect": [
            "corrupt 可能涨（负好感 + 不愿意 + 身体反应）",
            "submit 可能涨（驯化行为）",
        ],
    },
    "orgasm": {
        "desc": "高潮回落：到顶后回到中段，下一轮还能涨",
        "card": "sunna",
        "start": {
            "value": 80,
            "dev": {p: 70 for p in ("mouth", "nipple", "pussy", "anus", "skin")},
            "sense": {p: 80 for p in ("mouth", "nipple", "pussy", "anus", "skin")},
            "shame": 40,
            "excite": 88,
        },
        "lines": [
            # 同样要带反应：编剧判 orgasm 靠的是「她到了」这件事出现在文里。
            "（继续，节奏加快）她的呼吸断了，手指抓紧，背绷成一条线。",
            "（再继续）她整个人抖了一下，然后软下去，只剩喘。",
        ],
        "expect": [
            "excite 到顶后变成 50",
            "orgasm_count 至少 +1",
            "sense 整片 -10（且仍 >= dev）",
        ],
    },
}


# ─── 基础工具 ────────────────────────────────────────

def _persona():
    from app import persona
    return persona


def _adult():
    from app import affection_v2_adult as ad
    return ad


def _read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def _append_jsonl(path, row):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def _die(msg, code=2):
    print("✗ %s" % msg)
    raise SystemExit(code)


def _ok(msg):
    print("✓ %s" % msg)


def _card(card_id):
    """从卡库取一张卡（只读）。找不到就用第一张。"""
    data = _read_json(CARDS_PATH, [])
    if not isinstance(data, list):
        return None
    for c in data:
        if str(c.get("id")) == str(card_id):
            return c
    return data[0] if data else None


# ─── 副本管理 ────────────────────────────────────────

def _state_all(persona):
    return persona._load_state()


def _state_set(persona, key, ent):
    st = _state_all(persona)
    st[key] = ent
    persona._save_state(st)


def _make_clone(scenario_key, name):
    """建一条干净的副本线：新 key、指定卡、起始数值。"""
    persona = _persona()
    ad = _adult()
    sc = SCENARIOS[name]
    card = _card(sc.get("card"))
    if not card:
        _die("卡库读不到")

    key = (scenario_key or ("%s%s_%s" % (COPY_PREFIX, name,
                                          time.strftime("%H%M%S"))))[:64]
    if not SAFE_KEY.match(key) or not key.startswith(COPY_PREFIX):
        _die("副本 key 不合规：%r（必须以 %s 开头）" % (key, COPY_PREFIX))

    start = sc.get("start") or {}
    # 成人向状态：从默认值出发，叠加场景指定的
    ast = ad.empty_state()
    for f in ("excite", "corrupt", "submit", "shame", "control"):
        if f in start:
            ast[f] = int(start[f])
    for f in ("dev", "sense"):
        if isinstance(start.get(f), dict):
            ast[f].update({k: int(v) for k, v in start[f].items()})
    ast = ad.normalize(ast)

    # v2 关系档（schema=2），好感按场景给
    value = int(start.get("value", 0))
    aff = {
        "schema": 2,
        "value": value,
        "confess": 0,
        "yandere": 0,
        "contacts": [],
        "contact": 0,
        "adult": ast,
        "mode": "raid",
        "active": True,
    }

    ent = {
        "card": str(card.get("id") or ""),
        "card_name": str(card.get("name") or ""),
        "affection": aff,
        "state": "（测试副本，起点）",
        "mood": "",
        "intensity": 0,
        "turns": 0,
        "gameday": {"day": 1, "phase": "下午"},
    }
    _state_set(persona, key, ent)

    # 历史与归档都清空，跑出来的是纯新线
    hp = persona._hist_path(key)
    ap = persona._archive_path(key)
    for p in (hp, ap):
        if p:
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("")
    _ok("副本建好：%s（卡=%s，好感=%d）" % (key, card.get("name"), value))
    return key, card


def _adult_rec(ent):
    """成人向状态存在 affection.adult 下面（2026-11-08 定为唯一位置）。"""
    aff = (ent or {}).get("affection") or {}
    if isinstance(aff.get("adult"), dict):
        return aff
    # 兼容极早期写在顶层的副本
    if isinstance((ent or {}).get("adult"), dict):
        return ent
    return aff


def _snapshot(ent):
    ad = _adult()
    ast = ad.load(_adult_rec(ent))
    aff = (ent or {}).get("affection") or {}
    return {
        "value": aff.get("value"),
        "arousal": ast["arousal"],
        "excite": ast["excite"],
        "corrupt": ast["corrupt"],
        "submit": ast["submit"],
        "dev": dict(ast["dev"]),
        "sense": dict(ast["sense"]),
        "shame": ast["shame"],
        "control": ast["control"],
        "contact": ast["contact"],
        "orgasm_count": ast["orgasm_count"],
    }


def _diff(before, after):
    """两个快照的差异，打印用。"""
    out = []
    for k in ("arousal", "excite", "corrupt", "submit", "shame", "control",
              "contact", "orgasm_count", "value"):
        b, a = before.get(k), after.get(k)
        if b != a:
            out.append("  %-12s %s → %s" % (k, b, a))
    for k in ("dev", "sense"):
        b, a = before.get(k) or {}, after.get(k) or {}
        changed = {p: (b.get(p), a.get(p)) for p in a if b.get(p) != a.get(p)}
        if changed:
            out.append("  %-12s %s" % (k, ", ".join(
                "%s %s→%s" % (p, v[0], v[1]) for p, v in changed.items())))
    return "\n".join(out) or "  （无变化）"


# ─── 跑一轮 ──────────────────────────────────────────

def _run_one(key, text, idx):
    """跑一轮，把编剧 delta / 结算前后 / 注入块 / 正文全记下来。"""
    persona = _persona()
    ad = _adult()

    ent_before = persona._entry(key) or {}
    before = _snapshot(ent_before)

    # 抓这一轮编剧交给了什么：包一层 adult.apply，只观察不改行为
    captured = {}
    real_apply = ad.apply

    def spy(st, delta, value=None):
        captured["delta"] = copy.deepcopy(delta)
        captured["value_arg"] = value
        out, notes = real_apply(st, delta, value=value)
        captured["notes"] = list(notes)
        captured["after"] = {
            "arousal": out["arousal"], "excite": out["excite"],
            "corrupt": out["corrupt"], "submit": out["submit"],
            "shame": out["shame"], "control": out["control"],
            "dev": dict(out["dev"]), "sense": dict(out["sense"]),
            "contact": out["contact"], "orgasm_count": out["orgasm_count"],
        }
        return out, notes

    t0 = time.time()
    ad.apply = spy
    try:
        turn = persona.run_turn(key, text, "private", "")
    finally:
        ad.apply = real_apply
    dt = time.time() - t0

    ent_after = persona._entry(key) or {}
    after = _snapshot(ent_after)

    reply = (turn.reply or "").strip()
    block = ad.block(ad.load(_adult_rec(ent_after)))

    print("\n[%d] 你：%s" % (idx, text))
    print("  编剧 delta：%s" % json.dumps(captured.get("delta") or {}, ensure_ascii=False))
    if captured.get("notes"):
        for n in captured["notes"]:
            print("  ⚠ 闸门：%s" % n)
    print("  结算变化：")
    print(_diff(before, after))
    print("  她（%d 字，%.1fs）：%s" % (len(reply), dt,
                                    (reply[:80] + "…") if len(reply) > 80 else reply))
    print("  画面：%s" % (turn.scene or "（空）"))
    if block:
        print("  ── 注入块（前 3 行）")
        for line in block.splitlines()[1:4]:
            print("     " + line)

    _append_jsonl(os.path.join(REPLAY_DIR, key + ".jsonl"), {
        "i": idx,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": text,
        "reply": reply,
        "chars": len(reply),
        "elapsed": round(dt, 2),
        "scene": turn.scene or "",
        "delta": captured.get("delta") or {},
        "gate_notes": captured.get("notes") or [],
        "before": before,
        "after": after,
        "block": block,
    })
    return reply, before, after, captured


# ─── 断言 ────────────────────────────────────────────

def _check(name, before, after):
    """把场景里写的 expect 做成可自动判的检查。返回 (通过数, 失败列表)。"""
    ad = _adult()
    cfg = ad._cfg()
    fails = []
    checks = []

    def chk(desc, cond):
        checks.append(desc)
        if not cond:
            fails.append(desc)

    if name == "gating":
        cap = None
        for lv in cfg["arousal"]["levels"]:
            if int(lv.get("tier", 0)) == int(cfg["arousal"]["dev_gate"]["max_tier"]):
                cap = lv.get("key")
        order = [lv.get("key") for lv in
                 sorted(cfg["arousal"]["levels"], key=lambda x: int(x.get("tier", 0)))]
        cur = after["arousal"]
        if ad.dev_avg({"dev": after["dev"]}) < 40:
            chk("dev 均值 < 40 时冲动不超过 %s（实际 %s）" % (cap, cur),
                order.index(cur) <= order.index(cap))
        chk("正向好感时 corrupt 恒 0（实际 %s）" % after["corrupt"], after["corrupt"] == 0)
        chk("正向好感时 submit 恒 0（实际 %s）" % after["submit"], after["submit"] == 0)

    elif name == "warmup":
        chk("excite 有变化或已在回落后的中段",
            after["excite"] != before["excite"] or after["excite"] == 50)
        chk("sense 没有低于 dev",
            all(after["sense"][p] >= after["dev"][p] for p in after["dev"]))

    elif name == "shame_cap":
        chk("普通场景下羞耻不超过软顶 50（实际 %s）" % after["shame"],
            after["shame"] <= 50)

    elif name == "negative":
        chk("负好感时 corrupt 可以涨（%s → %s）" % (before["corrupt"], after["corrupt"]),
            after["corrupt"] >= before["corrupt"])
        chk("负好感时 submit 可以涨（%s → %s）" % (before["submit"], after["submit"]),
            after["submit"] >= before["submit"])

    elif name == "orgasm":
        chk("orgasm_count 增加了（%s → %s）"
            % (before["orgasm_count"], after["orgasm_count"]),
            after["orgasm_count"] > before["orgasm_count"])
        if after["orgasm_count"] > before["orgasm_count"]:
            chk("高潮后 excite 回到 50（实际 %s）" % after["excite"],
                after["excite"] == 50)

    # 全局不变量，任何场景都要过
    chk("sense 处处 >= dev",
        all(after["sense"][p] >= after["dev"][p] for p in after["dev"]))
    chk("所有数值都在合法区间",
        all(0 <= after[k] <= 100 for k in ("excite", "corrupt", "submit", "shame", "control")))

    return len(checks), fails


# ─── 命令 ────────────────────────────────────────────

def cmd_scenarios(args):
    print("可用场景：\n")
    for k, v in SCENARIOS.items():
        print("  %-12s %s" % (k, v["desc"]))
        st = v.get("start") or {}
        print("               起点：好感 %s" % st.get("value", 0))
        if "dev" in st:
            print("               开发：%s" % st["dev"])
        for e in v.get("expect") or []:
            print("               预期：%s" % e)
        print()


def cmd_run(args):
    name = args.name
    if name not in SCENARIOS:
        _die("没有这个场景：%s（用 scenarios 看列表）" % name)
    sc = SCENARIOS[name]

    key = args.key or ("%s%s_%s" % (COPY_PREFIX, name, time.strftime("%H%M%S")))
    if not key.startswith(COPY_PREFIX):
        _die("key 必须以 %s 开头" % COPY_PREFIX)

    persona = _persona()
    if persona._entry(key) and not args.force:
        _die("副本已存在：%s（加 --force 覆盖）" % key)

    key, card = _make_clone(key, name)
    ent0 = persona._entry(key) or {}
    first = _snapshot(ent0)
    print("\n起点：")
    print("  好感=%s 冲动=%s 高潮=%s 羞耻=%s 主动=%s"
          % (first["value"], first["arousal"], first["excite"],
             first["shame"], first["control"]))
    print("  开发：%s" % first["dev"])
    print("  敏感：%s" % first["sense"])

    for i, line in enumerate(sc["lines"], 1):
        _run_one(key, line, i)

    ent = persona._entry(key) or {}
    last = _snapshot(ent)
    if isinstance(ent.get("adult"), dict) and not isinstance(
            (ent.get("affection") or {}).get("adult"), dict):
        print("  ⚠ 警告：adult 写在顶层，和 run_turn 的写入位置不一致")
    total, fails = _check(name, first, last)

    print("\n" + "=" * 56)
    print("场景 %s 结果：%d/%d 项通过" % (name, total - len(fails), total))
    if fails:
        for f in fails:
            print("  ✗ %s" % f)
        print("\n记录：%s" % os.path.join(REPLAY_DIR, key + ".jsonl"))
        raise SystemExit(1)
    _ok("全部通过")
    print("记录：%s" % os.path.join(REPLAY_DIR, key + ".jsonl"))
    if not args.keep:
        print("（副本还在，看完用 drop %s 清掉）" % key)


def cmd_show(args):
    path = os.path.join(REPLAY_DIR, args.key + ".jsonl")
    rows = _read_jsonl(path)
    if not rows:
        _die("没有记录：%s" % path)
    for r in rows:
        print("\n[%s] 你：%s" % (r.get("i"), r.get("user")))
        print("  编剧 delta：%s" % json.dumps(r.get("delta") or {}, ensure_ascii=False))
        if r.get("gate_notes"):
            for n in r["gate_notes"]:
                print("  ⚠ %s" % n)
        b, a = r.get("before") or {}, r.get("after") or {}
        print(_diff(b, a))
        print("  她（%d 字）：" % r.get("chars", 0))
        for line in (r.get("reply") or "").splitlines():
            print("    " + line)
        print("  画面：%s" % (r.get("scene") or "（空）"))


def cmd_drop(args):
    key = args.key
    if not key.startswith(COPY_PREFIX):
        _die("只删 %s 开头的副本，拒绝动 %r" % (COPY_PREFIX, key))
    _drop_key(key)


def cmd_drop_all(args):
    persona = _persona()
    st = _state_all(persona)
    keys = [k for k in st if k.startswith(COPY_PREFIX)]
    for k in keys:
        _drop_key(k)
    if not keys:
        print("没有副本")


def _drop_key(key):
    persona = _persona()
    st = _state_all(persona)
    if key in st:
        del st[key]
        persona._save_state(st, deleted=[key])
    for p in (persona._hist_path(key), persona._archive_path(key)):
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    rp = os.path.join(REPLAY_DIR, key + ".jsonl")
    if os.path.exists(rp):
        try:
            os.remove(rp)
        except OSError:
            pass
    _ok("已清掉 %s" % key)


def main():
    ap = argparse.ArgumentParser(
        description="v2 成人向状态条真实轮次测试台（只跑副本，不碰真人线）")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("scenarios", help="列出所有场景")
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("run", help="跑一个场景")
    p.add_argument("name", help="场景名（见 scenarios）")
    p.add_argument("--key", default="", help="指定副本 key（默认自动生成）")
    p.add_argument("--force", action="store_true", help="覆盖同名副本")
    p.add_argument("--keep", action="store_true", help="跑完不提示清理")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("show", help="看副本的逐轮记录")
    p.add_argument("key")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("drop", help="清掉一个副本")
    p.add_argument("key")
    p.set_defaults(func=cmd_drop)

    p = sub.add_parser("drop-all", help="清掉所有副本")
    p.set_defaults(func=cmd_drop_all)

    args = ap.parse_args()
    if not getattr(args, "func", None):
        ap.print_help()
        return 1
    return args.func(args) or 0


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace")
    raise SystemExit(main())
