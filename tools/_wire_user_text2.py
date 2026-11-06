# -*- coding: utf-8 -*-
"""一次性：char_loras.apply 收 user_text；direct_gen 把用户原话传下去。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

# ① char_loras.apply 接住 user_text，判断用它，不用翻译稿
P1 = "app/char_loras.py"
s = open(P1, encoding="utf-8").read()
OLD = 'def apply(skill, prompt, workflow):'
NEW = 'def apply(skill, prompt, workflow, user_text=""):'
assert s.count(OLD) == 1
s = s.replace(OLD, NEW, 1)

OLD2 = '    entry = match(skill, prompt)'
NEW2 = ('    # 判断用**用户原话**（2026-11-06）：`prompt` 到这里已经是翻译好的\n'
        '    # 英文 tag，拿它去判等于让角色库读机器人自己写的文本。网页跑图台\n'
        '    # 没有「原话」，传空就退回用 prompt 本身，行为不变。\n'
        '    entry = match(skill, str(user_text or "").strip() or prompt)')
assert s.count(OLD2) == 1
s = s.replace(OLD2, NEW2, 1)
open(P1, "w", encoding="utf-8", newline="\n").write(s)
print("char_loras 已改")

# ② direct_gen 把 text（用户原话）传进 _generate_image
P2 = "app/direct_gen.py"
t = open(P2, encoding="utf-8").read()
OLD3 = ("        result = gi._generate_image(\n"
        "            prompt, skill=skill, _skip_confirm=True, **extra)")
NEW3 = ("        result = gi._generate_image(\n"
        "            prompt, skill=skill, _skip_confirm=True,\n"
        "            user_text=text, **extra)")
assert t.count(OLD3) == 1, t.count(OLD3)
t = t.replace(OLD3, NEW3, 1)
open(P2, "w", encoding="utf-8", newline="\n").write(t)
print("direct_gen 已改")
