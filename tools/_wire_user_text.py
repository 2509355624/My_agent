# -*- coding: utf-8 -*-
"""一次性：把 user_text 贯穿到两处 char_loras.apply（2026-11-06）。"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

P = "app/tools/normal/generate_image.py"
s = open(P, encoding="utf-8").read()

# ① QQ 直达生图那处
OLD1 = ("    from app import char_loras\n"
        "    workflow, prompt = char_loras.apply(skill, prompt, workflow)")
NEW1 = ("    from app import char_loras\n"
        "    workflow, prompt = char_loras.apply(skill, prompt, workflow,\n"
        "                                        user_text=user_text)")
# ② 网页跑图台/漫画/角色卡那处（build_t2i_workflow）
OLD2 = "def build_t2i_workflow(skill, prompt, seed):"
NEW2 = 'def build_t2i_workflow(skill, prompt, seed, user_text=""):'
OLD3 = ('    from app import char_loras\n'
        '    wf, prompt = char_loras.apply(skill, prompt, sd["workflow"])')
NEW3 = ('    from app import char_loras\n'
        '    wf, prompt = char_loras.apply(skill, prompt, sd["workflow"],\n'
        '                                  user_text=user_text)')

for old, new, tag in ((OLD1, NEW1, "直达"), (OLD2, NEW2, "签名"),
                      (OLD3, NEW3, "build")):
    n = s.count(old)
    assert n == 1, "%s 锚点 %d 处" % (tag, n)
    s = s.replace(old, new, 1)

open(P, "w", encoding="utf-8", newline="\n").write(s)
print("apply 带 user_text:", s.count("user_text=user_text"))
print("签名已改:", NEW2 in s)
