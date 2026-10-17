"""一次性验证脚本：用新的编剧规范真跑一遍「性转」需求，看 02/04/05 三格。

跑的是一话 5 格（跟用户那次的 C20261012-03 同规格），只打印结果、落一份
_tmp_verify_16.md 供人工比对。跑完即可删。
"""
import sys
import time

sys.path.insert(0, ".")
from app import comic_agent as A  # noqa: E402

brief = open("comics/C20261012-03/brief.txt", encoding="utf-8").read().strip()
print("[brief]", brief, flush=True)

t0 = time.time()


def prog(msg):
    print("[%6.1fs] %s" % (time.time() - t0, msg), flush=True)


try:
    text = A.write_script(brief, panels=5, progress=prog)
except Exception as e:  # noqa: BLE001
    print("[FAIL] %s: %s" % (type(e).__name__, e), flush=True)
    raise SystemExit(1)

print("[%6.1fs] 交稿，%d 字" % (time.time() - t0, len(text)), flush=True)
open("_tmp_verify_16.md", "w", encoding="utf-8").write(text)

_, panels = A.comic.parse_script_text(text)
print("\n" + "=" * 60, flush=True)
for p in panels:
    tags = A._scene_tags(p)
    print("## %s" % p["no"], flush=True)
    print("  画面: %s" % p["scene"], flush=True)
    print("  ① 多人标签 = %s" % (sorted(A._MULTI_PEOPLE & set(tags)) or "-"), flush=True)
    print("  ② 可辨发色 = %s" % sorted(A._appearance_colors(tags)), flush=True)
    povs = sorted({"pov", "pov_hands", "female_pov", "1other", "1boy"} & set(tags))
    print("  ③ 视角标签 = %s" % (povs or "-"), flush=True)
    print("  气泡: %s | %s" % (p.get("bubble", ""), p.get("bubble2", "")), flush=True)

print("=" * 60, flush=True)
issues = A.collect_issues(text, brief=brief)
print("代码判据剩余问题：%s" % (issues or "无"), flush=True)
print("check() =", A.check(text, 5, brief=brief)[0], flush=True)
