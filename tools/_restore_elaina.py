# -*- coding: utf-8 -*-
"""把伊蕾娜的进度从基础线搬回分线（2026-11-06 事故修复）。

事故：`_migrate` 一度返回 None，`_ensure` 把「读不到记录」当成新线，
新建空白记录并落盘，分线 `web_2509355624__elaina` 的好感/开发度被清零。
基础线 `web_2509355624`（同一张卡、同一段进度）没被读到，数据还在。
"""
import io
import json
import shutil
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

PATH = "state/persona_sessions.json"
BAK = PATH + ".bak-wipe-" + time.strftime("%Y%m%d-%H%M%S")

# 分线 → 数据来源（基础线）。只搬 affection，entry 上的账本/状态保留分线的。
PAIRS = [
    ("web_2509355624__elaina", "web_2509355624"),
]

with open(PATH, encoding="utf-8") as f:
    data = json.load(f)
shutil.copy2(PATH, BAK)
print("已备份:", BAK)

changed = []
for dst, src in PAIRS:
    d = data.get(dst)
    s = data.get(src)
    if not isinstance(d, dict) or not isinstance(s, dict):
        print("跳过（缺条目）:", dst)
        continue
    src_aff = s.get("affection")
    if not isinstance(src_aff, dict) or not src_aff.get("hist"):
        print("跳过（来源没有可信进度）:", src)
        continue
    old = d.get("affection") or {}
    new = json.loads(json.dumps(src_aff))
    new.pop("_prev", None)          # 回退快照是上一轮的，搬过来会错位
    d["affection"] = new
    changed.append(dst)
    print("%s 恢复：value %s→%s  level %s→%s  corrupt %s→%s  submit %s→%s" % (
        dst, old.get("value"), new.get("value"),
        old.get("level"), new.get("level"),
        old.get("corrupt"), new.get("corrupt"),
        old.get("submit"), new.get("submit")))
    print("   dev:", new.get("dev"))

if changed:
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print("已写入。恢复的线:", changed)
else:
    print("没有改动。")
