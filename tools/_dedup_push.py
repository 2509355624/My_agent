# -*- coding: utf-8 -*-
"""定向删掉重复的收尾段落（2026-11-06）。

上一版按「末尾连续相似」去重，没命中——因为重复不在末尾，后面还跟着
一段早晨的正文。这里改成**按内容特征**找：末尾 8 条里，凡是 assistant
且同时出现「一股热流」和「穴肉」的收尾段落，保留第一条，其余删掉。

先打印再删，数量对不上就中止。
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
KEY = "web_2509355624__elaina"
PATHS = {
    "窗口": os.path.join(BASE, "agents", "qq", "sessions",
                       "persona_%s.jsonl" % KEY),
    "归档": os.path.join(BASE, "agents", "qq", "sessions",
                       "persona_archive_%s.jsonl" % KEY),
}
STATE = os.path.join(BASE, "state", "persona_sessions.json")

MARK = ("一股热流", "穴肉")


def load(p):
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def is_finish_row(m):
    if str(m.get("role")) != "assistant":
        return False
    c = str(m.get("content") or "")
    return all(k in c for k in MARK)


def pick(rows, window=8):
    """返回要删的下标：末尾 window 条里，除了第一条之外的收尾段落。"""
    start = max(0, len(rows) - window)
    hits = [i for i in range(start, len(rows)) if is_finish_row(rows[i])]
    return hits[1:] if len(hits) > 1 else []


def save(p, rows, tag):
    bak = p + ".bak-dedup2-" + time.strftime("%H%M%S")
    shutil.copy2(p, bak)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        for m in rows:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print("%s 剩 %d 条（备份 %s）" % (tag, len(rows), os.path.basename(bak)))


drops = {}
rowsets = {}
for tag, p in PATHS.items():
    rows = load(p)
    idx = pick(rows)
    print("%s 命中要删的下标：%s" % (tag, idx))
    for i in idx:
        print("   - %s" % str(rows[i].get("content"))[:50].replace("\n", " "))
    drops[tag] = idx
    rowsets[tag] = rows

if len(drops["窗口"]) != len(drops["归档"]):
    print("⚠️ 两边不一致，停下")
    sys.exit(1)
if not drops["窗口"]:
    print("没有可删的")
    sys.exit(0)

n = len(drops["窗口"])
for tag, p in PATHS.items():
    rows = rowsets[tag]
    for i in sorted(drops[tag], reverse=True):
        rows.pop(i)
    save(p, rows, tag)

state = json.load(open(STATE, encoding="utf-8"))
ent = state[KEY]
ent["turn_total"] = max(0, int(ent.get("turn_total") or 0) - n)
json.dump(state, open(STATE, "w", encoding="utf-8", newline="\n"),
          ensure_ascii=False, indent=2)
print("删了 %d 条，turn_total -> %s" % (n, ent["turn_total"]))
