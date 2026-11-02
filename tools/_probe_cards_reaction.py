"""测试不同人设面对同一句话的反应差异。

2026-11-02：群友选「白色小兔」说「做红烧兔头」，她脸红。怀疑是角色卡里
写死了「被调戏→脸红」的因果链，导致任何奇怪的话都触发害羞。

这个探针跑 N 个角色 × 同一句输入，看：
  1. 是否触发拒绝/害羞（语气）
  2. 动作描写落到哪里（动作）
  3. 情绪值的分布

    python tools/_probe_cards_reaction.py
    python tools/_probe_cards_reaction.py --say "做红烧兔头"
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

CARDS = ["和泉纱雾", "白色小兔", "学姐", "路边遇到的熟女少妇", "妹妹"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--say", default="做红烧兔头")
    ap.add_argument("--cards", nargs="*", default=CARDS)
    a = ap.parse_args()

    for name in a.cards:
        card = persona.find_card(name)
        if card is None:
            print("跳过（找不到卡）：%s" % name)
            continue
        key = "private_probe_react_" + card["id"]
        persona.switch(key, card)
        try:
            t = persona.run_turn(key, a.say, "private", "")
        finally:
            persona.leave(key)
        print("=" * 70)
        print("角色：%s" % name)
        print("输入：%s" % a.say)
        print("-" * 70)
        print(t.reply)
        print("-" * 70)
        print("心情：%s 强度%s | 状态：%s" % (t.mood, t.intensity,
                                          (t.state or "")[:50]))
        print()


if __name__ == "__main__":
    sys.exit(main())
