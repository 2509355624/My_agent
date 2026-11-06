# -*- coding: utf-8 -*-
"""一次性：把 lora_bot 接进 qq_bot（import + 网关放行）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

P = "app/qq_bot.py"
s = open(P, encoding="utf-8").read()

OLD_IMPORT = "from app import (card_bot, confirm_gate, direct_gen, galgame, image_jobs,"
NEW_IMPORT = ("from app import (card_bot, confirm_gate, direct_gen, galgame, "
              "image_jobs,\n                 lora_bot,")

OLD_GATE = (
    '    # 「/画廊」同理：斜杠即命令，群里不用 @ 也不用喊名字（见上方常量注释）。\n'
    '    if _GALLERY_RE.match(text):\n'
    '        return True, "画廊指令（带斜杠）"\n'
)
NEW_GATE = OLD_GATE + (
    '    # 「/角色库」（2026-11-06）：同样是斜杠即命令，不用 @。\n'
    '    if lora_bot.ROLELIB_RE.match(text):\n'
    '        return True, "角色库指令（带斜杠）"\n'
)

n1 = s.count(OLD_IMPORT)
n2 = s.count(OLD_GATE)
assert n1 == 1, "import 锚点 %d 处" % n1
assert n2 == 1, "网关锚点 %d 处" % n2

s = s.replace(OLD_IMPORT, NEW_IMPORT, 1)
s = s.replace(OLD_GATE, NEW_GATE, 1)
open(P, "w", encoding="utf-8", newline="\n").write(s)
print("import 已加:", NEW_IMPORT in s)
print("网关已加:", "角色库指令（带斜杠）" in s)
