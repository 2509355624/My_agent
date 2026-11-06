# -*- coding: utf-8 -*-
"""一次性：把 krea2 从归档里放出来（2026-11-06）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

P = "app/skills.py"
s = open(P, encoding="utf-8").read()

OLD = '    + ["krea2"]\n    + ["moss", "moss8", "moss50", "moss_page"]'
NEW = ('    # 2026-11-06：krea2 也放出来——用户点名用它却回落到 silver，那种\n'
       '    # 「渠道串号」就是这么来的：归档 = list_skills 不吐它 = QQ 的\n'
       '    # _allowed_skills 里没有 = direct_gen 认不出 = 静默降级成默认档。\n'
       '    + ["moss", "moss8", "moss50", "moss_page"]')

n = s.count(OLD)
assert n == 1, "锚点 %d 处" % n
s = s.replace(OLD, NEW, 1)
open(P, "w", encoding="utf-8", newline="\n").write(s)
print("已改")
