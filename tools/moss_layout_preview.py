# -*- coding: utf-8 -*-
"""把一次出整本的 Layout JSON 画成线框图，看分镜和文字位置。

    python tools/moss_layout_preview.py state/_selfcheck/oneshot_xxx.json
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def kind_of(it):
    c = (it.get("caption") or "").lower()
    if it.get("label") != "text":
        return "art"
    if "boy plain text" in c:
        return "boy"
    if "speech bubble" in c:
        return "bubble"
    if "sound effect" in c:
        return "sfx"
    return "narr"


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page_svg(p, idx):
    W, H = 1024, 1536
    cells = [i for i in p.get("items") or [] if i.get("label") in ("character", "object")]
    texts = [i for i in p.get("items") or [] if i.get("label") == "text"]
    out = ['<svg class="page" viewBox="-512 -768 1024 1536" xmlns="http://www.w3.org/2000/svg">']
    out.append('<rect x="-512" y="-768" width="1024" height="1536" fill="#fff" stroke="#111" stroke-width="6"/>')
    for n, c in enumerate(cells, 1):
        y0, x0, y1, x1 = c["bbox"]
        out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="%s" stroke="#111" stroke-width="4"/>'
                   % (x0, y0, x1 - x0, y1 - y0,
                      "#f2f2f2" if c.get("label") == "object" else "#fafafa"))
        out.append('<text x="%d" y="%d" font-size="34" fill="#bbb" font-family="sans-serif">%d</text>'
                   % (x0 + 14, y0 + 40, n))
        cap = (c.get("caption") or "")
        # 只取景别+动作那一段（到外形串之前的第一个句号后两个词）
        brief = cap.split(". ")[0][:46]
        out.append('<text x="%d" y="%d" font-size="24" fill="#999" font-family="sans-serif">%s</text>'
                   % (x0 + 14, y0 + 78, esc(brief)))
    for t in texts:
        y0, x0, y1, x1 = t["bbox"]
        k = kind_of(t)
        w, h = x1 - x0, y1 - y0
        if k == "bubble":
            out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="%d" fill="#fff" stroke="#111" stroke-width="3"/>'
                       % (x0, y0, w, h, min(w, h) // 2))
        elif k == "boy":
            out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#e00" stroke-width="2" stroke-dasharray="8 6"/>'
                       % (x0, y0, w, h))
        elif k == "sfx":
            out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="none" stroke="#888" stroke-width="2" stroke-dasharray="3 5"/>'
                       % (x0, y0, w, h))
        else:
            out.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#fff" stroke="#111" stroke-width="2"/>'
                       % (x0, y0, w, h))
        txt = t.get("text") or ""
        n = max(1, len(txt))
        # 竖排：先按 40 估计行数，再按行×列反算字号，保证不溢出框
        fs = 40
        rows = max(1, int((h - 16) / fs))
        cols = max(1, -(-n // rows))
        fs = int(min(52, (h - 12) / rows, (w - 12) / cols))
        fs = max(14, fs)
        per = -(-n // cols)
        for ci in range(cols):
            seg = txt[ci * per:(ci + 1) * per]
            cx = x1 - 26 - ci * fs
            for li, ch in enumerate(seg):
                out.append('<text x="%d" y="%d" font-size="%d" fill="%s" text-anchor="middle" font-family="serif">%s</text>'
                           % (cx, y0 + 24 + li * fs, fs,
                              "#e00" if k == "boy" else "#111", esc(ch)))
    out.append("</svg>")
    label = '<div class="cap">第 %d 页 · %s · %d 格 / %d 条文字</div>' % (
        idx, p.get("layout") or "?", len(cells), len(texts))
    return '<div class="col">%s%s</div>' % (label, "".join(out))


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        d = os.path.join(ROOT, "state", "_selfcheck")
        src = max((os.path.join(d, f) for f in os.listdir(d) if f.startswith("oneshot_") and f.endswith(".json")),
                  key=os.path.getmtime)
    with open(src, encoding="utf-8") as f:
        js = json.load(f)
    cols = "".join(page_svg(p, i) for i, p in enumerate(js.get("pages") or [], 1))
    html = """<!doctype html><meta charset="utf-8"><title>%s</title>
<style>
body{margin:0;background:#f6f6f4;font-family:-apple-system,"Segoe UI",sans-serif;color:#222}
h1{font-size:20px;padding:20px 24px 6px;margin:0}
.sub{padding:0 24px 14px;color:#777;font-size:13px}
.row{display:flex;gap:20px;padding:0 24px 30px;overflow-x:auto;align-items:flex-start}
.col{flex:0 0 300px}
.cap{font-size:12px;color:#666;padding:6px 2px 8px}
.page{width:300px;height:450px;background:#fff;border-radius:4px;box-shadow:0 2px 10px rgba(0,0,0,.12)}
</style>
<h1>%s</h1><div class="sub">%d 页 · 一次请求产出 · 红虚线＝男主画外裸字，圆角＝女生气泡，直角＝旁白</div>
<div class="row">%s</div>""" % (
        esc(js.get("title") or "MOSS"), esc(js.get("title") or "MOSS"),
        len(js.get("pages") or []), cols)
    dst = os.path.splitext(src)[0] + "_preview.html"
    with open(dst, "w", encoding="utf-8") as f:
        f.write(html)
    print(dst)


if __name__ == "__main__":
    main()
