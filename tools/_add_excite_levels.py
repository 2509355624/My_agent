# -*- coding: utf-8 -*-
"""给 config 补 `excite_levels`（兴奋度四档）。

2026-11-04 晚：用户要的第二根轴——「兴奋度就是进行那个行为的时候，
女主的一个状态」。结构（几档、区间、给模型的话）定在这里，**具体文案
随时可以整段换掉**：改 config、重启 5174 就生效，不用动代码。

可重放：已经有 excite_levels 就整段覆盖成这里的版本。
"""
import io
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "affection_tiers.json")

LEVELS = [
    {
        "key": "high", "min": 75, "name": "快不行了",
        "prompt": "话已经说不完整了，声音发飘。会主动凑上来，也会小声提要求——"
                  "这时候她提要求不是撒娇，是顾不上别的了。动作比她平时快，"
                  "也不再管自己是不是太主动。",
    },
    {
        "key": "mid", "min": 50, "name": "跟着你走",
        "prompt": "已经进去了，反应跟着你的节奏走。你说什么她做什么，"
                  "偶尔自己动一下，做完又想装作是没忍住。还会害羞，但害羞"
                  "已经拦不住身体了。",
    },
    {
        "key": "low", "min": 25, "name": "有点发紧",
        "prompt": "开始有反应了。呼吸变浅，会下意识抓住身边的东西——"
                  "被子、你的手、自己的衣角。嘴上可能还在逞强。",
    },
    {
        "key": "zero", "min": 0, "name": "还没开始",
        "prompt": "刚进来，还没缓过来。动作有点僵，需要一点时间，"
                  "也可能随时喊停。",
    },
]


def main():
    with io.open(PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    d["excite_levels"] = LEVELS
    tmp = PATH + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, PATH)
    print("excite_levels =", [(x["key"], x["min"], x["name"]) for x in LEVELS])


if __name__ == "__main__":
    main()
