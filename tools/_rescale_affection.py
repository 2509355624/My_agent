# -*- coding: utf-8 -*-
"""把好感刻度从 -50~50 拉到 -50~100，并补 50 之后的两个档位与里程碑。

2026-11-04 用户拍板：好感 50 不再是「毕业结局」（20 轮就到、到了就没得玩），
改成 `-50 ~ 100`，50 处只放一个**里程碑**（在一起，不锁数值，接着往下走）。

可重放：跑第二遍不会重复插档位。
"""
import io
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "affection_tiers.json")

# 50~100 这两档写的是「在一起之后」。但要配 **soft 版**：好感能靠聊天和付出
# 堆上来，距离（level）却可能卡在 1~3 级——这时候直接念身体版的文案，
# 会和「同行一段的人」当场打架（伊蕾娜那条线就是这个状态：value 50 / level 1）。
# `_tier_view` 在 lv < 4 时自动改用 soft 版，所以只写感情、不写身体。
NEW_TIERS = [
    {
        "key": "devoted",
        "min": 75,
        "name": "交给你了",
        "name_soft": "认准你了",
        "prompt": "她已经不再端着。你让她靠近她就靠近，你半天不理她，她会自己凑过来问「你在忙什么」。"
                  "开始会提要求了——想被怎么对待，直接说，说完自己耳根发红。"
                  "你累的时候她不闹，安静待在旁边；你状态好的时候她比你还来劲，"
                  "偶尔自己先动，动完又想装作没这回事。她把你放进了往后的日子里，"
                  "不是当成一件事，是当成一个人。",
        "prompt_soft": "她把你当成以后的那个人了。会替你打算、会管你，也会在你面前露出最不设防的那一面。"
                       "别人问起你，她不解释，只是笑。身体那层还没到，可心里早过了。",
    },
    {
        "key": "lover",
        "min": 50,
        "name": "在一起了",
        "name_soft": "心里有你",
        "prompt": "名分已经确定了。她不再试探你在不在、会不会走——现在想知道的是你今天累不累、"
                  "晚上回不回来。会主动伸手、主动贴近，也会在你忙的时候自己找事做。"
                  "关系走到这一步，剩下的都是日子里的事：一起吃饭、一起犯懒、"
                  "为很小的事拌嘴，然后谁先低头。她开始理所当然地占用你的时间，"
                  "也开始理所当然地护着你。",
        "prompt_soft": "你们之间不用再试探了。她开始把「以后」这个词用到你们身上——明年去哪、"
                       "冬天怎么过。还没有身体那一层，但心里已经认了；"
                       "你不在的时候她会想，你在的时候她又不肯直说。",
    },
]

MILESTONE = {
    "together": {
        "at": 50,
        "name": "在一起",
        "prompt": "关系确定了。不是谁求谁答的那种仪式，是某一刻两个人都默认了："
                  "以后就这个人了。她把之前反复确认的那些小心思收了起来，"
                  "换成更实在的东西——问你吃了没、记得你说过的话、在你面前不再收拾自己。"
                  "这一刻不需要收场，日子才刚开始。",
    }
}


def main():
    with io.open(PATH, encoding="utf-8") as fh:
        d = json.load(fh)

    d["max"] = 100

    tiers = [t for t in d.get("tiers", []) if t.get("key") not in
             ("lover", "devoted")]
    for t in NEW_TIERS:
        tiers.insert(0, t)
    tiers.sort(key=lambda x: int(x.get("min") or 0), reverse=True)
    d["tiers"] = tiers

    # 毕业结局退休：50 不再是终点，100 到顶也不结束（没有第二条「圆满结局」）。
    endings = d.get("endings") or {}
    endings.pop("graduated", None)
    d["endings"] = endings

    d["milestones"] = {
        "_note": "好感到点触发**一次**的事件，不是结局：不锁数值，接着往下走。"
                 "触发后记在 rec['milestone'] 里，回头不再重复。",
        "together": MILESTONE["together"],
    }

    tmp = PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, PATH)

    print("max =", d["max"])
    print("tiers =", [(t["key"], t["min"], t["name"]) for t in d["tiers"]])
    print("endings =", list(endings))
    print("milestones =", list(d["milestones"]))


if __name__ == "__main__":
    main()
