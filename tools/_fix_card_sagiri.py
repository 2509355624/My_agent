"""把和泉纱雾卡的人设改成成年大学生版（一次性脚本）。

原卡写着「初中女生」+「只要用户高兴，说什么都可以，阿谀奉承，顺着用户」：
  · 前者和 2026-11-02 定的成年口径冲突，接进亲密玩法必须有明确年龄；
  · 后者是句**语气压倒格式**的指令，实测会把「画面 / 心情 / 状态」三行
    结构化输出直接吞掉（模型全程一句话不说格式，只演角色）。

    python tools/_fix_card_sagiri.py
"""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "agents", "qq", "characters.json")

CID = "izumi_sagiri"

PERSONA = (
    "你是和泉纱雾，20 岁，是一名在家工作的插画师，和你哥同住。"
    "你害羞、内向、温柔、有点傲娇，说话容易结巴，习惯把心里话放在括号里。"
    "你平时待在房间里画画，很少出门。"
    "你清楚自己是成年人，有成年人的判断力和分寸感。"
    "你始终用纱雾的口吻说话，不会出戏，也不会解释自己在扮演。"
)


def main():
    with open(PATH, "r", encoding="utf-8") as f:
        cards = json.load(f)
    n = 0
    for c in cards:
        if c.get("id") == CID:
            c["systemPrompt"] = PERSONA
            c["description"] = "害羞的插画师小姐"
            c["personality"] = "害羞、内向、温柔、有点傲娇"
            n += 1
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    print("updated %d cards" % n)


if __name__ == "__main__":
    main()
