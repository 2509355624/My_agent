# -*- coding: utf-8 -*-
"""一次性：把 doing 参数接进 /api/persona/skip_day（2026-11-06）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

P = "app/persona_api.py"
src = open(P, encoding="utf-8").read()

OLD = (
    "    try:\n"
    "        days = max(1, min(365, int(body.get(\"days\") or 1)))\n"
    "    except (TypeError, ValueError):\n"
    "        days = 1\n"
)
NEW = OLD + (
    "    # 这段时间在干什么（2026-11-06）：有值就按它写过程，\n"
    "    # 而不是把一段旅程压缩成「几天后到了」。\n"
    "    doing = str(body.get(\"doing\") or \"\").strip()[:200]\n"
)

assert src.count(OLD) == 1, "锚点不唯一：%d" % src.count(OLD)
src = src.replace(OLD, NEW, 1)
for who in ("ckey", "key"):
    src = src.replace(
        'turn = persona.skip_day(%s, "", days=days)' % who,
        'turn = persona.skip_day(%s, "", days=days, doing=doing)' % who)

open(P, "w", encoding="utf-8").write(src)
print("doing 定义：", src.count("doing = str(body.get"))
print("传参：", src.count("doing=doing"))
