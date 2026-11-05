# -*- coding: utf-8 -*-
"""把伊蕾娜线退回「射精弹窗刚弹出」的那一刻（2026-11-06）。

起因：修 bug 时我在浏览器里连点了三次「确定」，每次都真的生成了一段，
于是历史里出现连续的 assistant 气泡。现在把多出来的那些删掉，回到
弹窗等答案的状态。

做法：
  1. 删掉窗口和归档末尾**连续的 assistant 条**（只删测试多出来的那几条）；
  2. turn_total 跟着减同样条数；
  3. 状态恢复成「刚到高潮、还没回答射在哪」：excite=余韵值、recover=3、
     scene=True、pending_finish={n: 当前第几次}。

先停 agent.py 再跑，否则内存快照会盖回来。
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
sys.path.insert(0, BASE)

KEY = "web_2509355624__elaina"
HIST = os.path.join(BASE, "agents", "qq", "sessions",
                    "persona_%s.jsonl" % KEY)
ARCH = os.path.join(BASE, "agents", "qq", "sessions",
                    "persona_archive_%s.jsonl" % KEY)
STATE = os.path.join(BASE, "state", "persona_sessions.json")

DROP = 2          # 我测试时多生成的段数


def _tail(path):
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def _rewrite(path, rows):
    bak = path + ".bak-undo-" + time.strftime("%H%M%S")
    shutil.copy2(path, bak)
    with open(path, "w", encoding="utf-8") as f:
        for m in rows:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    print("  备份:", os.path.basename(bak))


def _cut(rows, drop, tag):
    """只删末尾连续的 assistant 条，且不超过 drop。"""
    n = 0
    while rows and n < drop and str(rows[-1].get("role")) == "assistant":
        rows.pop()
        n += 1
    print("%s 删掉 %d 条，剩 %d 条" % (tag, n, len(rows)))
    return n


from app import affection  # noqa: E402

hist = _tail(HIST)
arch = _tail(ARCH)
n1 = _cut(hist, DROP, "窗口")
n2 = _cut(arch, DROP, "归档")
if n1 != n2:
    print("⚠️ 窗口和归档删除条数不一致，先别写，人工看一下")
    sys.exit(1)
_rewrite(HIST, hist)
_rewrite(ARCH, arch)

state = json.load(open(STATE, encoding="utf-8"))
ent = state[KEY]
aff = ent.get("affection") or {}
after = affection.after_value(aff.get("dev") or {})
n = int(aff.get("orgasms") or 1)
aff.update({"excite": after, "scene": True, "idle": 0,
            "recover": affection.ORGASM_RECOVER})
ent["affection"] = aff
ent["turn_total"] = max(0, int(ent.get("turn_total") or 0) - n1)
ent["pending_finish"] = {"n": n, "of": 5, "last": n >= 5}
json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False,
          indent=2)

print("状态：excite=%s recover=%s pending=%s turn_total=%s"
      % (aff["excite"], aff["recover"], ent["pending_finish"],
         ent["turn_total"]))
