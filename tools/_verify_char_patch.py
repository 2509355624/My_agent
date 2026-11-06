# -*- coding: utf-8 -*-
"""真装填：拿每个架构的真实 workflow，挂对应角色，看节点插对没有。

    D:\\AI\\confyui_env\\Scripts\\python.exe tools\\_verify_char_patch.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import char_loras, skills  # noqa: E402

CASES = [
    ("silver", "nanally", "anima\\异环\\nte-nanally.safetensors"),
    ("miao", "jinhsi", "sdxl\\wuwa-jinhsi.safetensors"),
    ("cunny", "aria", "sdxl\\zzz-wingxiang-v4.safetensors"),
    ("image_gen_v1", "zhezhi", "sd15\\wuwa-zhezhi.safetensors"),
    ("moss", "gemini-moss", "moss\\gemini-moss-v1.safetensors"),
]

fails = []
for skill, key, want_file in CASES:
    p = os.path.join(skills.SKILLS_DIR, skill, "workflow.json")
    if not os.path.isfile(p):
        print("%-14s 没有 workflow.json" % skill)
        fails.append(skill)
        continue
    wf = skills.load_workflow(p)
    before = len(wf)
    out, prompt = char_loras.apply(skill, "1girl, solo, standing", wf,
                                   user_text="测试", key=key)
    node = out.get("char_lora") or {}
    got = str((node.get("inputs") or {}).get("lora_name") or "")
    ok = (node.get("class_type") == "LoraLoaderModelOnly"
          and got.replace("/", "\\") == want_file)
    # 触发词必须压尾、identity 打头
    ent = char_loras.entry_by_key(key, skill) or {}
    trig_tail = prompt.rstrip().endswith(str(ent.get("trigger") or ""))
    ident_head = prompt.startswith(str(ent.get("identity") or "x")[:20])
    print("%-14s +%-2d 节点 %-18s %s" % (
        skill, len(out) - before, got.split("\\")[-1],
        "✓" if ok else "✗ 期望 " + want_file))
    if not (ok and trig_tail and ident_head):
        print("     trigger 压尾=%s identity 打头=%s" % (trig_tail, ident_head))
        fails.append(skill)

# 原 workflow 不能被改
p = os.path.join(skills.SKILLS_DIR, "miao", "workflow.json")
wf = skills.load_workflow(p)
src = json.dumps(wf, sort_keys=True)
char_loras.apply("miao", "1girl", wf, user_text="测试", key="jinhsi")
same = src == json.dumps(wf, sort_keys=True)
print("模板未被改：%s" % ("✓" if same else "✗"))
if not same:
    fails.append("模板被改")

print("失败 %d" % len(fails), fails)
sys.exit(1 if fails else 0)
