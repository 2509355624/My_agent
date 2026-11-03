"""检查 persona.html 的布局兜底是否齐全（2026-11-03）。

群友反馈「看不到打字的输入框」。根因是 `.app { height: 100dvh }`——
`dvh` 是 Chrome 108 才有的单位，QQ 内置浏览器/老安卓 WebView 不认，
整条声明被丢弃后 `.app` 没有高度，页面退回文档流，输入框被顶到视口外。

这里做静态检查：三层兜底在不在、flex 收缩属性在不在。
不需要浏览器，正则扫一遍源码就够。
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "web", "persona.html")

CHECKS = [
    ("`.app` 有 position: fixed 兜底",
     r"\.app\s*\{[^}]*position:\s*fixed"),
    ("`.app` 有 100vh 兜底（dvh 之前）",
     r"\.app\s*\{[^}]*height:\s*100vh"),
    ("`.app` 有 100dvh",
     r"\.app\s*\{[^}]*height:\s*100dvh"),
    ("`.app` 用 var(--vh) 接软键盘高度",
     r"\.app\s*\{[^}]*height:\s*var\(--vh"),
    ("`.log` 有 min-height: 0（不然撑高挤走输入框）",
     r"\.log\s*\{[^}]*min-height:\s*0"),
    ("`.composer` 有 flex: none（不被压缩）",
     r"\.composer\s*\{[^}]*flex:\s*none"),
    ("有 visualViewport 监听",
     r"visualViewport"),
    ("有 `--vh` 写入",
     r'setProperty\(\s*"--vh"'),
    ("有 viewport-fit=cover（不然 env() 恒为 0）",
     r'viewport-fit=cover'),
    ("输入区底部让出安全区",
     r"env\(safe-area-inset-bottom\)"),
    ("flex gap 有老浏览器兜底（Chromium < 84 没有 gap）",
     r"@supports not \(gap"),
    ("grid 有 flex 兜底（Chromium < 57 没有 grid）",
     r"@supports \(display: grid\)"),
]


def main():
    with open(PATH, encoding="utf-8") as f:
        html = f.read()
    # 去掉注释再做匹配，免得注释里的示例被误判成真实规则
    body = re.sub(r"/\*.*?\*/", "", html, flags=re.S)

    bad = 0
    for name, pat in CHECKS:
        ok = re.search(pat, body, re.S) is not None
        print("  %s %s" % ("✓" if ok else "✗", name))
        if not ok:
            bad += 1

    print()
    if bad:
        print("有 %d 项缺失" % bad)
        return 1
    print("布局兜底齐全。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
