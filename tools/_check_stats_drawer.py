"""手机端状态抽屉的结构自检（2026-11-04 深夜）。一次性脚本。"""
import io
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
P = r"D:\AI\agent_my_test\web\persona.html"
s = io.open(P, encoding="utf-8").read()

checks = [
    ("btnStats 元素", 'id="btnStats"' in s),
    ("btnStats 无内联 display:none",
     not re.search(r'id="btnStats"[^>]*display:\s*none', s)),
    ("宽屏隐藏 !important", ".top .stats-btn { display: none !important; }" in s),
    ("窄屏显示", ".top .stats-btn { display: inline-block; }" in s),
    ("side2 窄屏变右抽屉", "transform: translateX(100%)" in s),
    ("side2 打开态", ".side2.open { transform: none; }" in s),
    ("openStats 定义", "function openStats()" in s),
    ("closeStats 定义", "function closeStats()" in s),
    ("btnStats 绑定", '$("btnStats").onclick = openStats' in s),
    ("遮罩关两个抽屉", "closeDrawer(); closeStats();" in s),
    ("左右互斥(openDrawer)", "closeStats();                       // 左右互斥" in s),
    ("renderMode 管 btnStats", 'const s = $("btnStats")' in s),
    ("旧的 display:none 已移除",
     "@media (max-width: 1000px) { .side2 { display: none; } }" not in s),
]

bad = 0
for name, ok in checks:
    print("  %s %s" % ("OK  " if ok else "FAIL", name))
    if not ok:
        bad += 1
print()
print("全部通过" if not bad else "有 %d 项失败" % bad)
sys.exit(1 if bad else 0)
