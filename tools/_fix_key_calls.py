"""把 persona_api 里的 `key = _key(who)` 换成 `_active_key(_key(who))`。

2026-11-03：切角色 = 换一条独立会话线（`<base>__<card_id>`）。
除 `switch` 那个函数外，其余读取/写历史的接口都要走 `_active_key`。
"""
import io
import os
import re

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "app", "persona_api.py")

with io.open(PATH, encoding="utf-8") as f:
    s = f.read()

# switch 里已经单独处理过（base / ckey 两条线），不能一起替换。
n = s.count("    key = _key(who)\n")
s = s.replace("    key = _key(who)\n",
              "    key = _active_key(_key(who))\n")

with io.open(PATH, "w", encoding="utf-8") as f:
    f.write(s)
print("替换 %d 处" % n)
