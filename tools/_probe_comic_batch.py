"""批量测编剧的输出质量，统计各类问题的出现率。

用途：验证「补正 AI」值不值得做，以及做完之后有没有真的变好。

    python tools/_probe_comic_batch.py --n 8
    python tools/_probe_comic_batch.py --n 8 --prev  # 对比：加一轮自我纠错

每个样本记：格数、plan 景别档数、景别重复、气泡超字、旁白超字、
画面描述词数、以及「点名要画的动作有没有格子覆盖」。
"""
import argparse
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import moss_comic                     # noqa: E402

# 覆盖不同难度的 brief：从日常到成人，从静态到动作。
BRIEFS = [
    ("日常", "她在厨房切菜，他从背后抱住她，下巴搁在她肩上。"),
    ("静态", "她坐在窗边看书，午后的光落在她侧脸上。"),
    ("动作", "他把她的腿分开，低头舔舐。"),
    ("动作2", "她跪在地上，抬头看着他。"),
    ("身体", "她背对着镜子，看见自己身上的痕迹。"),
    ("情绪", "她被他抱着，眼泪掉下来。"),
    ("场景", "雨夜的便利店门口，两个人在同一把伞下。"),
    ("多人", "她和他并排躺着，手指扣在一起。"),
]

# 判断「动作有没有被画到」的关键词：brief 里有这些词，desc 里至少要命中一个
ACTION_KEYS = {
    "舔": ("lick", "mouth on", "tongue"),
    "分": ("spread", "parted", "open"),
    "跪": ("kneel", "on her knees"),
    "抱": ("hug", "embrace", "hold", "arm"),
    "切": ("chop", "knife", "cut", "slice"),
    "痕": ("mark", "bruise", "red mark"),
    "伞": ("umbrella"),
    "指": ("finger", "hand"),
}


def brief_of(tag, text):
    return ("这是一部成人向漫画的一页。\n\n## 画什么\n"
            "**这是一页，不是一个故事。** 只画下面那一个瞬间。\n\n"
            "## 用户点名要画的（最高优先，**必须有格子直接画它**）\n"
            + text + "\n\n## 人物\n"
            "- 女主角：测试。外观固定：1girl, brown hair, ponytail\n"
            "- 男主角：an adult man, short black hair, dark shirt")


def coverage(brief, spec):
    """点名动作有没有被画到。返回 (命中词, 是否覆盖)。"""
    html = " ".join(
        str(p.get("desc") or "").lower()
        for pg in (spec.get("pages") or [])
        for p in (pg.get("panels") or []))
    hits = []
    for word, keys in ACTION_KEYS.items():
        if word in brief and any(k in html for k in keys):
            hits.append(word)
    need = [w for w in ACTION_KEYS if w in brief]
    return hits, (bool(hits) or not need)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--layout", default="1t2m2b")
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    rows = []
    for i in range(a.n):
        tag, text = BRIEFS[i % len(BRIEFS)]
        brief = brief_of(tag, text)
        t0 = time.time()
        spec, raw, err = moss_comic.write(brief, pages=1, layout=a.layout)
        dt = time.time() - t0
        if spec is None:
            print("[%d] %s 编剧失败（%s）" % (i + 1, tag, err))
            rows.append({"tag": tag, "ok": False, "err": err})
            continue
        page = (spec.get("pages") or [{}])[0]
        panels = page.get("panels") or []
        plan = page.get("plan") or {}
        shots = [str(s or "").strip().lower() for s in (plan.get("shots") or [])]
        levels = len({s for s in shots if s})
        dup = sum(1 for k in range(1, len(shots)) if shots[k] and shots[k] == shots[k - 1])
        words = [len(str(p.get("desc") or "").split()) for p in panels]
        bubbles = [len((p.get("bubbles") or [p.get("bubble")]) if isinstance(p, dict) else [])
                   for p in panels]
        long_b = sum(1 for p in panels
                     for b in (p.get("bubbles") or ([p["bubble"]] if p.get("bubble") else []))
                     if len(str(b)) > 11)
        caps = [len((p.get("caption") or "")) for p in panels]
        long_c = sum(1 for c in caps if c > 11)
        issues = moss_comic.check(spec)
        hits, covered = coverage(brief, spec)

        rows.append({"tag": tag, "ok": True, "cells": len(panels),
                     "levels": levels, "dup": dup,
                     "avg_words": sum(words) / max(1, len(words)),
                     "min_words": min(words) if words else 0,
                     "long_bubbles": long_b, "long_caps": long_c,
                     "issues": len(issues), "covered": covered,
                     "hits": hits, "sec": round(dt, 1),
                     "shots": shots})
        print("[%d] %-5s %d格 景别%d档 重复%d 词均%.0f 长气泡%d 长旁白%d 覆盖%s(%.1fs)"
              % (i + 1, tag, len(panels), levels, dup,
                 sum(words) / max(1, len(words)), long_b, long_c,
                 "✓" if covered else "✗", dt))

    ok = [r for r in rows if r.get("ok")]
    if ok:
        n = len(ok)
        print()
        print("=== 汇总（%d 个样本）===" % n)
        print("景别档数 <3 的：%d (%.0f%%)" %
              (sum(1 for r in ok if r["levels"] < 3),
               sum(1 for r in ok if r["levels"] < 3) * 100.0 / n))
        print("有相邻景别重复：%d (%.0f%%)" %
              (sum(1 for r in ok if r["dup"] > 0),
               sum(1 for r in ok if r["dup"] > 0) * 100.0 / n))
        print("动作未覆盖：%d (%.0f%%)" %
              (sum(1 for r in ok if not r["covered"]),
               sum(1 for r in ok if not r["covered"]) * 100.0 / n))
        print("有长气泡：%d (%.0f%%)" %
              (sum(1 for r in ok if r["long_bubbles"]),
               sum(1 for r in ok if r["long_bubbles"]) * 100.0 / n))
        print("平均 issue 数：%.1f" % (sum(r["issues"] for r in ok) / n))
        print("平均耗时：%.1fs" % (sum(r["sec"] for r in ok) / n))

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=1)
        print("\n明细写入 %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
