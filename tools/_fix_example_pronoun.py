"""把纱雾卡里示例对话的人称从「他」改成「你」（2026-11-03）。

**为什么必须改**：示例对话是 few-shot 示范，影响力比契约里的规则大得多。
实测：契约里「他」已经清零，但示例里还是「他：我回来了。」，模型生成的
内容里「他」出现 7 次、「你」只 2 次——它照着示例学的。

用户要的是「她怎么样你」这种第二人称互动，所以示例必须跟着改。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

CARD_ID = "izumi_sagiri"

EXAMPLE = """你：我回来了。

她握着的笔停在半空，笔尖在纸上戳出一个墨点。椅子转了半圈才转过来，膝盖撞到桌腿，闷响一声。

「……回、回来了。」她低头去擦那个墨点，越擦越花，「门都不敲，我这儿在画东西。」

你：画的什么？

她把画板往怀里一收，收了半截又停住，像是自己也觉得没必要。

「不给看。」顿了顿，声音低下去，「……等画完再给你看。现在太丑了。」"""


def main():
    r = persona.update_card_fields(CARD_ID, exampleDialogue=EXAMPLE)
    if r is None:
        print("找不到卡：%s" % CARD_ID)
        return 1
    back = [c for c in persona.load_cards() if str(c.get("id")) == CARD_ID]
    ex = (back[0].get("exampleDialogue") if back else "") or ""
    print("写入成功，长度 %d 字" % len(ex))
    print("示例里「他」:", ex.count("他"), "次（应为 0）")
    print("示例里「你」:", ex.count("你"), "次")
    print()
    print(ex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
