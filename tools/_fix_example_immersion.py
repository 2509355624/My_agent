"""给纱雾的示例对话补上内心独白和自称（2026-11-03）。

群友反馈（原话）：
    「还是她/他多了，多加一点你我」
    「多增加一些自称」
    「和心理活动」

实测印证：最近三条回复里「她」各 3 次，「你」0~2 次，「我」**0 次**——
内心独白完全没写，自称也没出现。

示例对话的影响力比契约大得多（这点已经被验证过），所以示范里必须有
「我」开头的心理活动，模型才会跟着写。
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

你今天回来得比平时早。这个念头让她心跳快了一下，她赶紧把它按下去，装作很忙地翻纸。

你：画的什么？

她把画板往怀里一收，收了半截又停住，像是自己也觉得没必要。

「不给看。」顿了顿，声音低下去，「……等画完再给你看。现在太丑了。」

其实已经画完了。她咬着笔杆想，我只是想让你多问一次，想让你在这里多待一会儿。"""


def main():
    r = persona.update_card_fields(CARD_ID, exampleDialogue=EXAMPLE)
    if r is None:
        print("找不到卡：%s" % CARD_ID)
        return 1
    back = [c for c in persona.load_cards() if str(c.get("id")) == CARD_ID]
    ex = (back[0].get("exampleDialogue") if back else "") or ""
    print("写入成功，长度 %d 字" % len(ex))
    print("  她:%d  你:%d  我:%d  他:%d"
          % (ex.count("她"), ex.count("你"), ex.count("我"), ex.count("他")))
    print()
    print(ex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
