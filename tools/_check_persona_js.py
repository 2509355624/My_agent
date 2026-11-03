"""检查 persona.html 的 JS 结构与关键绑定（2026-11-03）。

用 node 跑一遍语法检查（如果有 node），再做文本层面的关键点确认。
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "web", "persona.html")

KEYS = [
    "let CARD_MAP",
    "data-cid",
    'addEventListener("click"',
    'addEventListener("touchend"',
    "CARD_MAP[String(c.id)]",
    "function pick",
    "function loadCards",
]


def main():
    with open(PATH, encoding="utf-8") as f:
        html = f.read()
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        print("找不到 script 块")
        return 1
    js = m.group(1)

    print("花括号 %d/%d  %s"
          % (js.count("{"), js.count("}"),
             "平衡" if js.count("{") == js.count("}") else "**不平衡**"))
    print("圆括号 %d/%d  %s"
          % (js.count("("), js.count(")"),
             "平衡" if js.count("(") == js.count(")") else "**不平衡**"))
    print()

    bad = 0
    for k in KEYS:
        ok = k in js
        print("  %s %s" % ("OK " if ok else "缺", k))
        if not ok:
            bad += 1

    # 有 node 就做真语法检查
    node = None
    for cand in ("node", "node.exe"):
        try:
            subprocess.run([cand, "--version"], capture_output=True,
                           timeout=10, check=True)
            node = cand
            break
        except Exception:                              # noqa: BLE001
            continue

    print()
    if node:
        tmp = os.path.join(ROOT, "state", "_js_check.js")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(js)
        r = subprocess.run([node, "--check", tmp], capture_output=True,
                           text=True, timeout=30, encoding="utf-8",
                           errors="replace")
        ok = r.returncode == 0
        print("node --check:", "通过" if ok else "**失败**")
        if not ok:
            print((r.stderr or "")[:800])
            bad += 1
        try:
            os.remove(tmp)
        except OSError:
            pass
    else:
        print("（没装 node，跳过语法检查；只做了文本层面确认）")

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
