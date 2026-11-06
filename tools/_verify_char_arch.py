# -*- coding: utf-8 -*-
"""验证按架构分渠道的角色库：同一个 key 在 anima / miao / image_gen_v1 下取到不同文件。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import char_loras  # noqa: E402

PAIRS = [
    # key, 渠道, 期望 file 片段
    ("nanally-v2", "silver", "nte-nanally-v2"),
    ("nanally-v2", "hd_3_clear", "nte-nanally-v2"),
    ("miyako", "anima-line", "ba-miyako"),
    ("gemini", "silver-hd", "gemini-anima"),
    ("gemini-moss", "moss", "gemini-moss"),
    ("jinhsi", "miao", "wuwa-jinhsi"),
    ("aria", "cunny", "zzz-wingxiang"),
    ("chinatsu", "hd_3_clear", "zzz-chinatsu"),      # anima 版（原有）
    ("chinatsu", "miao", "zzz-wingxiang"),           # SDXL 版（本次）
    ("rossi", "silver", "endfield-rossi"),           # anima 版（原有）
    ("rossi", "nffa", "endfield-rossi"),             # SDXL 版（本次，同文件名不同目录）
    ("zhezhi", "image_gen_v1", "wuwa-zhezhi"),
    ("yukikaze", "nsfw_pose_gen", "azur-yukikaze"),
]

BAD = [
    # 该渠道不该拿到这个 key
    ("nanally", "miao"),        # anima 专属，miao 不挂
    ("miyako", "krea2-general"),
    ("zhezhi", "miao"),         # SD1.5 的不能进 SDXL 渠道
]

fails = []
for key, skill, want in PAIRS:
    e = char_loras.entry_by_key(key, skill)
    ok = bool(e) and want in str(e.get("file"))
    acc = char_loras._accept(skill, e) if e else None
    print("%-14s %-14s %-42s %s" % (
        key, skill, (e or {}).get("file", "-"), "✓" if ok and acc else "✗"))
    if not (ok and acc):
        fails.append((key, skill, (e or {}).get("file")))

print("--- 反例（应当拿不到）")
for key, skill in BAD:
    e = char_loras.entry_by_key(key, skill)
    acc = char_loras._accept(skill, e) if e else None
    good = acc is None
    print("%-14s %-14s %-42s %s" % (key, skill, (e or {}).get("file", "-"),
                                    "✓" if good else "✗ 错挂"))
    if not good:
        fails.append((key, skill, "误挂"))

print("--- 白名单")
for grp, names in (("anima", char_loras._LORA_SKILLS_ANIMA),
                   ("sdxl", char_loras._LORA_SKILLS_SDXL),
                   ("sd15", char_loras._LORA_SKILLS_SD15),
                   ("moss", char_loras._LORA_SKILLS_MOSS)):
    print("   %-6s %d 个" % (grp, len(names)), ",".join(names))

print("失败 %d" % len(fails), fails)
sys.exit(1 if fails else 0)
