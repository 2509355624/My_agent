"""扮演态端到端探针：跑一轮，判断要不要出图，要出就直接调 draw。

**不连 QQ**，只验证「门槛 → 提示词 → 入队 → 拿到文件名」这条链。
ComfyUI 没在线时会打印跳过原因。

    python tools/_probe_persona_draw.py --say "..." --force
"""
import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

SESSION = "private_10001"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", default="和泉纱雾")
    ap.add_argument("--say", default="我回来了。")
    ap.add_argument("--force", action="store_true", help="不看门槛，强制出图")
    ap.add_argument("--reply-to", default="7000000000000001")
    a = ap.parse_args()

    card = persona.find_card(a.card)
    if card is None:
        print("找不到角色卡：%s" % a.card)
        return 1
    persona.switch(SESSION, card)
    card = persona.active_card(SESSION)

    prev = str((persona._entry(SESSION) or {}).get("state") or "")  # noqa: SLF001
    t0 = time.time()
    turn = persona.run_turn(SESSION, a.say, "private", "")
    print("=== 正文 ===\n%s" % turn.reply)
    print("\n=== 结构化 ===")
    print("画面：%s" % turn.scene)
    print("情绪：%s 强度 %s" % (turn.mood, turn.intensity))
    print("状态：%s" % turn.state)

    want = a.force or persona.should_draw(turn.intensity, prev, turn.state)
    print("\n=== 出图门槛：%s（强度 %s，prev_state=%r）==="
          % (want, turn.intensity, prev[:30]))
    if not want:
        print("这轮不出图，结束。要强制看链路就加 --force")
        persona.leave(SESSION)
        return 0
    if not turn.scene:
        print("没有画面行，persona.draw 会直接返回空")
    prompt = persona.build_prompt(card, turn.scene, turn.state)
    print("=== 实际出图提示词 ===\n%s" % prompt)
    name = persona.draw(turn.scene, card, SESSION, reply_to=a.reply_to,
                        state=turn.state)
    print("\n=== 出图结果 ===\nfile: %s\n耗时 %.1fs" % (name or "(空)", time.time() - t0))
    print("caption: %s" % persona.image_caption(turn.reply))
    persona.leave(SESSION)
    return 0


if __name__ == "__main__":
    sys.exit(main())
