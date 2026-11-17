# -*- coding: utf-8 -*-
"""把 anima 步数 A/B 的两张图拼成并排对比：左=基线(10步)，右=实验(20步)。

每对输出一张竖条：整图缩略 | 头部 1:1 | 下半身 1:1。

用法： python _anima_ab_compare.py 00479:00484 00475:00485 ...
       （左是基线 tag，右是新图 tag）
"""
import os
import sys
from PIL import Image, ImageDraw

OUT = r"D:\AI\ComfyUI_v037\output"
DST = r"D:\AI\agent_my_test\_face_crops"


def load(tag):
    return Image.open(os.path.join(OUT, "Anima_%s_.png" % tag)).convert("RGB")


def band(im, y0, y1, x0=0.16, x1=0.84, width=520):
    W, H = im.size
    c = im.crop((int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H)))
    return c.resize((width, int(width * c.height / c.width)), Image.LANCZOS)


def build(pairs):
    os.makedirs(DST, exist_ok=True)
    for base_tag, new_tag in pairs:
        a, b = load(base_tag), load(new_tag)
        thumb_w = 260
        ta = a.resize((thumb_w, int(thumb_w * a.height / a.width)), Image.LANCZOS)
        tb = b.resize((thumb_w, int(thumb_w * b.height / b.width)), Image.LANCZOS)

        rows = [("整图", ta, tb),
                ("头部", band(a, 0.06, 0.30), band(b, 0.06, 0.30)),
                ("下半身", band(a, 0.55, 1.00), band(b, 0.55, 1.00))]

        pad, hdr = 10, 26
        row_h = [max(r[1].height, r[2].height) + hdr for r in rows]
        W = pad * 3 + thumb_w * 2
        H = pad + sum(row_h) + pad
        sheet = Image.new("RGB", (W, H), (22, 22, 22))
        d = ImageDraw.Draw(sheet)
        y = pad
        for (name, ia, ib), rh in zip(rows, row_h):
            d.text((pad, y + 5), "%s   左：10 步（%s）    右：20 步（%s）" % (name, base_tag, new_tag),
                   fill=(255, 220, 120))
            sheet.paste(ia, (pad, y + hdr))
            sheet.paste(ib, (pad * 2 + thumb_w, y + hdr))
            y += rh
        p = os.path.join(DST, "_ab_%s_vs_%s.png" % (base_tag, new_tag))
        sheet.save(p)
        print("写好:", p, sheet.size)


if __name__ == "__main__":
    build([s.split(":") for s in sys.argv[1:]])
