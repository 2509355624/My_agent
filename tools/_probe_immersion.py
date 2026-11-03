"""实测沉浸感改动（2026-11-03）：人称分布、内心独白、反引号。

群友反馈：「还是她/他多了，多加一点你我」「多增加一些自称」「和心理活动」。

跑法：
    python tools/_probe_immersion.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

KEY = "web_90005"
SAYS = [
    "我从背后抱住你，下巴搁在你肩上。",
    "手顺着你的腰往下滑。",
]


def main():
    card = persona.find_card("和泉纱雾")
    if card is None:
        print("找不到卡")
        return 1

    # 清掉上次的痕迹
    for p in (persona._hist_path(KEY), persona._archive_path(KEY)):   # noqa: SLF001
        if p and os.path.isfile(p):
            os.remove(p)

    persona.switch(KEY, card)
    try:
        for said in SAYS:
            persona.run_turn(KEY, said, "private", "")
        t = persona.run_turn(KEY, "把脸贴过来。", "private", "")
    finally:
        persona.leave(KEY)

    r = t.reply
    print("=== %d 字 ===" % len(r))
    print(r)
    print()
    n_she = r.count("她")
    n_you = r.count("你")
    n_me = r.count("我")
    n_he = r.count("他")
    backtick = r.count(chr(96))
    print("人称分布：她=%d  你=%d  我=%d  他=%d"
          % (n_she, n_you, n_me, n_he))
    print("反引号：%d（应为 0）" % backtick)

    # 判据按「群友要什么」来定，不按「数字好看」来定：
    #   1. 「他」必须 0        —— 那是残留，会破坏代入
    #   2. 「你」要多于「她」  —— 读者在场
    #   3. 至少一处「我」      —— 进得去她的脑子
    #   4. 没有标记符号        —— 内心独白不该被包起来
    checks = [
        ("「他」清零", n_he == 0),
        ("「你」多于「她」", n_you > n_she),
        ("至少一处「我」", n_me >= 1),
        ("无标记符号", backtick == 0),
    ]
    print()
    bad = 0
    for name, ok in checks:
        print("  %s %s" % ("✓" if ok else "✗", name))
        if not ok:
            bad += 1
    print()
    print("判据：", "✓ 达标" if bad == 0 else ("✗ %d 项不达标" % bad))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
