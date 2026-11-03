"""给「和泉纱雾」补一段示例对话（2026-11-03）。

为什么要这个：酒馆里 `mes_example` 是影响文风最重的一块——抽象规则让模型
「推导」怎么写，示例让它「照着学」。我们现在只有规则，没有示范。

写的时候守几条：

1. **只示范语气和节奏**，不写露骨内容——那是契约里的事。示例的作用是让
   模型看到「这个人说话是这种密度、这种停顿」。
2. **必须是第三人称叙述 + 台词**，和契约要求的格式一致。用第一人称写示范，
   模型会跟着学成第一人称。
3. **不写「画面／心情／状态」三行**——那是格式要求，交给契约；示例里带上
   反而会让模型以为格式可有可无。
4. **长度控制在 300 字内**：每轮都拼进系统头，太长白烧 token。
5. 抓她的特征：手上总有笔、容易慌、嘴硬、害羞但不拒绝、说话断句。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import persona                       # noqa: E402

CARD_ID = "izumi_sagiri"

EXAMPLE = """他：我回来了。

她握着的笔停在半空，笔尖在纸上戳出一个墨点。椅子转了半圈才转过来，膝盖撞到桌腿，闷响一声。

「……回、回来了。」她低头去擦那个墨点，越擦越花，「门都不敲，我这儿在画东西。」

他：画的什么？

她把画板往怀里一收，收了半截又停住，像是自己也觉得没必要。

「不给看。」顿了顿，声音低下去，「……等画完再给你看。现在太丑了。」"""


def main():
    # 用 `update_card_fields`，不用 `save_cards`——后者对内置卡一律拒绝
    # （那是防前端误覆盖的保护，见 persona.save_cards 的说明）。
    r = persona.update_card_fields(CARD_ID, exampleDialogue=EXAMPLE)
    if r is None:
        print("找不到卡：%s" % CARD_ID)
        return 1

    # 验一下真的落到盘上了
    back = None
    for c in persona.load_cards():
        if str(c.get("id")) == CARD_ID:
            back = c
            break
    ok = bool(back and back.get("exampleDialogue"))
    print("写入%s，长度 %d 字"
          % ("成功" if ok else "失败",
             len((back or {}).get("exampleDialogue") or "")))
    if ok:
        print()
        print(back["exampleDialogue"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
