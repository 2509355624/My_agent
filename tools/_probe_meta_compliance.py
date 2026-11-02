"""验证假设：正文越长，模型越容易漏写「画面/心情/状态」三行。

2026-11-02：契约涨到 3k 字后，漏三行的概率从「基本不丢」升到约 1/3。
推测不是上下文不够（实际输入才 2 万字），而是长输出后格式指令被正文
指导挤掉了。如果是这样，正文长度和漏格式应该正相关。

跑法：
    python tools/_probe_meta_compliance.py --rounds 10

每轮换一句话（从短到长），记录正文长度、三行是否齐全，最后给相关系数。
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

# 从轻到重排列：越往后越要求详细描写，正文自然越长。
SAYS = [
    "我回来了。",
    "你今天一直在画画吗？",
    "我坐到你旁边，看了一眼屏幕上的画。",
    "我从背后抱住你，下巴搁在你肩膀上。",
    "我把你的椅子转过来，让你面对我。",
    "我低头，在你耳边说话，手搭在你腰上。",
    "我把你抱起来放到床上，压上去。",
    "我解开你的衣扣，一颗一颗，慢慢往下。",
    "我把你的T恤从头顶拽下来，手贴上你的后背。",
    "我把你按在床上，从后面贴着你的耳朵说话。",
]


def _pct(ok, total):
    return 0.0 if not total else ok * 100.0 / total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=10)
    ap.add_argument("--card", default="和泉纱雾")
    a = ap.parse_args()

    card = persona.find_card(a.card)
    if card is None:
        print("找不到卡：%s" % a.card)
        return 1
    key = "private_probe_meta"
    persona.switch(key, card)

    rows = []
    try:
        for i, said in enumerate(SAYS[:a.rounds], 1):
            t = persona.run_turn(key, said, "private", "")
            full = bool(t.scene) and bool(t.mood) and bool(t.state)
            n = len(t.reply)
            rows.append((n, full))
            print("[%02d] %3d 字  三行%s  %s" % (
                i, n, "齐" if full else "缺", said[:24]))
    finally:
        persona.leave(key)

    if not rows:
        return 1
    ok = sum(1 for _, f in rows if f)
    print()
    print("完整率：%d/%d = %.0f%%" % (ok, len(rows), _pct(ok, len(rows))))

    full_lens = [n for n, f in rows if f]
    miss_lens = [n for n, f in rows if not f]
    if full_lens:
        print("齐的正文平均长度：%.0f 字（%d 条）"
              % (sum(full_lens) / len(full_lens), len(full_lens)))
    if miss_lens:
        print("缺的正文平均长度：%.0f 字（%d 条）"
              % (sum(miss_lens) / len(miss_lens), len(miss_lens)))

    # 点到双列相关（长度 vs 是否完整）
    if len(rows) >= 3 and len(set(n for n, _ in rows)) > 1:
        xs = [n for n, _ in rows]
        ys = [1.0 if f else 0.0 for _, f in rows]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        dx = sum((x - mx) ** 2 for x in xs) ** 0.5
        dy = sum((y - my) ** 2 for y in ys) ** 0.5
        r = num / (dx * dy) if dx and dy else 0.0
        print("相关系数 r = %.2f（负值 = 正文越长越容易漏）" % r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
