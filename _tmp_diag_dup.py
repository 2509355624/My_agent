"""诊断：为什么 C20261012-05 的补正稿被闸门拒收。

只读，不改任何东西。跑法：
    D:\AI\confyui_env\Scripts\python.exe _tmp_diag_dup.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import comic, comic_agent as ca

P = os.path.join("comics", "C20261012-05", "script_original.md")
text = io.open(P, encoding="utf-8").read()
brief = io.open(os.path.join("comics", "C20261012-05", "brief.txt"),
                encoding="utf-8").read()

print("=== collect_issues（原稿）===")
for it in ca.collect_issues(text, brief):
    print(" ", it["no"], it["kind"], it.get("tag"), "|", it["detail"][:40])

print()
print("=== duplicate_wearables 逐格拆解 ===")
_, panels = comic.parse_script_text(text)
for p in panels:
    tags = ca._scene_tags(p)
    multi = ca._MULTI_PEOPLE & set(tags)
    hits = {}
    for tag in tags:
        for cat in sorted(ca._tag_tokens(tag) & ca._WEARABLE_WORDS):
            hits.setdefault(cat, []).append(tag)
    dup = {k: v for k, v in hits.items() if len(v) > 1}
    if not dup and not multi:
        continue
    print("格 %s  人数标签=%s  _MULTI_PEOPLE命中=%s" % (p["no"], sorted(multi) or "-",
                                                  bool(multi)))
    for k, v in dup.items():
        print("     重复类别 `%s` ← %s" % (k, v))
