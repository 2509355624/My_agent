# -*- coding: utf-8 -*-
"""按用户口述回填一条线的数值（2026-11-06）。

背景：`web_2509355624__elaina` 被 `_migrate` 的 bug 清空过。基础线
`web_2509355624` 是分线那一刻的旧快照（堕落 17 / 屈服 20），所以从它
恢复只能回到旧值；分线上后来涨到满的那份已经不在任何备份里。
用户确认原值是：堕落满、屈服满、冲动满、高潮「快要」。

用法（先停 agent.py / qq_bot，否则内存快照会盖回来）：
    python tools/_set_line_values.py
"""
import io
import json
import os
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(BASE, "state", "persona_sessions.json")

# 线键 → 要写死的字段。只写列出来的，其余一律不动。
TARGET = {
    "web_2509355624__elaina": {
        "corrupt": 100,
        "submit": 100,
        "arousal": 100,
        "excite": 75,        # 「快要」= edge 档的起点
        "scene": True,       # 高潮值 > 0 必须在场景里，否则面板不显示
        "idle": 0,
    },
}


def main():
    with open(PATH, encoding="utf-8") as f:
        data = json.load(f)
    bak = PATH + ".bak-setvals-" + time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(PATH, bak)
    print("已备份:", os.path.basename(bak))

    changed = []
    for key, vals in TARGET.items():
        ent = data.get(key)
        if not isinstance(ent, dict):
            print("跳过（没有这条线）:", key)
            continue
        aff = ent.get("affection")
        if not isinstance(aff, dict):
            print("跳过（没有好感记录）:", key)
            continue
        before = {k: aff.get(k) for k in vals}
        aff.update(vals)
        # 基础线是分线的镜像，一起改，免得两边显示不一致。
        base = key.split("__", 1)[0]
        if isinstance(data.get(base), dict) and isinstance(
                data[base].get("affection"), dict):
            data[base]["affection"].update(vals)
        changed.append(key)
        print("%s：%s → %s" % (key, before, vals))

    if not changed:
        print("没有改动。")
        return
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print("已写回。")


if __name__ == "__main__":
    main()
