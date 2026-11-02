"""扮演态一轮的离线探针：直接调 persona.run_turn，看契约有没有被遵守。

不出图、不连 QQ，只验证三件事：
  1. 模型会不会老老实实输出「画面：」「心情：」「状态：」三行；
  2. `split_turn` 能不能把它们剥干净（正文里不许残留）；
  3. `should_draw` 给出的判断合不合理。

    python tools/_probe_persona_mood.py --card 和泉纱雾 --say "我回来了"
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

SESSION = "private_10000"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="和泉纱雾")
    ap.add_argument("--say", default="我回来了。")
    a = ap.parse_args()

    card = persona.find_card(a.card)
    if card is None:
        print("找不到角色卡：%s" % a.card)
        return 1
    persona.switch(SESSION, card)
    card = persona.active_card(SESSION)
    print("=== 角色：%s（%s）===" % (card["name"], card["id"]))
    print("=== 人设 ===\n%s" % card["systemPrompt"][:300])

    head = persona.build_head(card, SESSION, "", "")
    print("\n=== system 头长度：%d 字 ===" % len(head))

    turn = persona.run_turn(SESSION, a.say, "private", "")
    print("\n=== 正文 ===")
    print(turn.reply)
    print("\n=== 结构化字段 ===")
    print("画面：%s" % turn.scene)
    print("情绪：%s 强度 %s" % (turn.mood, turn.intensity))
    print("状态：%s" % turn.state)

    prev = str((persona._entry(SESSION) or {}).get("state") or "")  # noqa: SLF001
    print("\n=== 落盘检查 ===")
    print("state 已存：%s" % (prev[:80] or "(空)"))

    # 第二轮：验证状态有没有被喂回去
    turn2 = persona.run_turn(SESSION, "我把外套递给你。", "private", "")
    print("\n=== 第二轮正文 ===")
    print(turn2.reply)
    print("情绪：%s 强度 %s | 状态：%s" % (turn2.mood, turn2.intensity, turn2.state))
    print("要出图吗（强度>=7 或换衣）：%s" % persona.should_draw(
        turn2.intensity, prev, turn2.state))

    persona.leave(SESSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
