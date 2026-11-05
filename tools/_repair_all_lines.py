# -*- coding: utf-8 -*-
"""全库检修：把所有人被清空/错乱的状态条尽量修回来（2026-11-06）。

做四件事，每一步都只**补**不**删**：

1. **口径归一**：跑一遍 `_migrate`，把开发度从旧的 500 制收到 100 制、
   `body` 改名 `skin`、缺字段补齐。
2. **分线抢救**：`web_<qq>__<card>` / `private_<qq>__<card>` 如果好感记录
   被清空（没有 hist），而基础线 `web_<qq>` / `private_<qq>` 上还留着同一张
   卡的进度（有 hist），就把好感搬过去。伊蕾娜那次就是这么救回来的。
3. **天数对齐**：基础线和分线天数不一致时取**大的**，两边都写回去。
   天数被旧快照盖回去过一次的线，靠这一步抬回来。
4. **报警**：有账本/轮次却没有好感记录的线，逐条列出来——这些没有可搬的
   来源，只能人工补。

用法：
    python tools/_repair_all_lines.py --dry     # 只报告，不写
    python tools/_repair_all_lines.py           # 真写（会先备份）
注意：**先停掉 agent.py / qq_bot**，否则它们的内存快照可能把改动盖回去。
"""
import io
import json
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
PATH = os.path.join(BASE, "state", "persona_sessions.json")

import app.affection as affection  # noqa: E402


def _day(ent):
    try:
        return int((ent.get("gameday") or {}).get("day") or 0)
    except (TypeError, ValueError):
        return 0


def _has_hist(ent):
    a = ent.get("affection")
    return isinstance(a, dict) and bool(a.get("hist"))


def _pair_of(key):
    """分线键 → 基础键。不是分线就返回 None。"""
    if "__" not in key:
        return None
    return key.split("__", 1)[0]


def main():
    dry = "--dry" in sys.argv
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)

    if not dry:
        bak = PATH + ".bak-repair-" + time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(PATH, bak)
        print("已备份:", os.path.basename(bak))

    changed, rescued, days_fixed, lost = [], [], [], []

    for key in sorted(data):
        ent = data.get(key)
        if not isinstance(ent, dict):
            continue

        # 1) 口径归一
        aff = ent.get("affection")
        if isinstance(aff, dict):
            before = json.dumps(aff, sort_keys=True, ensure_ascii=False)
            aff2 = affection._migrate(json.loads(before))      # noqa: SLF001
            if aff2 is not None and json.dumps(
                    aff2, sort_keys=True, ensure_ascii=False) != before:
                ent["affection"] = aff2
                changed.append(key)

        # 2) 分线抢救
        base = _pair_of(key)
        if base and base in data:
            src, dst = data[base], ent
            if (_has_hist(src) and not _has_hist(dst)
                    and str(src.get("card")) == str(dst.get("card"))):
                new = json.loads(json.dumps(src["affection"]))
                new.pop("_prev", None)
                dst["affection"] = new
                rescued.append((key, new.get("value"), new.get("dev")))
                changed.append(key)

        # 3) 天数对齐（两边都往大写）
        #    只处理**真玩过**的线：账本、轮次、好感历史，有一个就算。
        #    而且**只在同一张卡之间对齐**——基础线是某一具体卡的镜像，
        #    拿它的天数去抬另一张卡的线，会凭空给那张卡安一个天数。
        base = _pair_of(key)
        played = bool(str(ent.get("note") or "").strip()) or _has_hist(ent)
        try:
            played = played or int(ent.get("turns") or 0) > 0 \
                or int(ent.get("turn_total") or 0) > 0
        except (TypeError, ValueError):
            pass
        if (base and base in data and played
                and str(data[base].get("card")) == str(ent.get("card"))):
            other = data[base]
            d1, d2 = _day(ent), _day(other)
            if d2 > d1:
                ent["gameday"] = json.loads(json.dumps(other.get("gameday") or {}))
                ent.pop("gameday_prev", None)
                days_fixed.append((key, d1, d2))
                changed.append(key)
            elif d1 > d2:
                other["gameday"] = json.loads(json.dumps(ent.get("gameday") or {}))
                other.pop("gameday_prev", None)
                days_fixed.append((base, d2, d1))
                changed.append(base)

        # 4) 报警
        note = str(ent.get("note") or "").strip()
        try:
            turns = int(ent.get("turns") or 0)
            total = int(ent.get("turn_total") or 0)
        except (TypeError, ValueError):
            turns = total = 0
        if (note or turns or total) and not _has_hist(ent):
            lost.append((key, len(note), total or turns))

    print()
    print("=== 抢救回来的分线（%d）===" % len(rescued))
    for k, v, dev in rescued:
        print("  %-40s value=%-5s dev=%s" % (k[:40], v, dev))
    print()
    print("=== 天数抬回来的线（%d）===" % len(days_fixed))
    for k, a, b in days_fixed:
        print("  %-40s %s → %s" % (k[:40], a, b))
    print()
    print("=== 没有可搬来源、需要人工补的线（%d）===" % len(lost))
    for k, nlen, t in lost:
        print("  %-40s 账本%4d字 轮次%s" % (k[:40], nlen, t))
    print()
    print("有改动的键 %d 个" % len(set(changed)))

    if dry:
        print("\n[dry] 没有写盘。去掉 --dry 才会写。")
        return
    if not changed:
        print("\n没有需要写的改动。")
        return
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("\n已写回。备份在 state/ 下（.bak-repair-*）。")


if __name__ == "__main__":
    main()
