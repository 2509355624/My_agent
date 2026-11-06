# -*- coding: utf-8 -*-
"""给「鸣潮全角色」包里用户点名的这几个角色建索引条目（2026-11-06）。

一个文件（wuwa-all-anima-v2.safetensors）里 47 个角色，靠触发句区分。
这里只收用户问到的 5 个，identity 用包作者给的原始 tag（衣服已拆到 wardrobe）。
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
INDEX = os.path.join(BASE, "config", "char_loras.json")
PACK = "anima\\鸣潮\\wuwa-all-anima-v2.safetensors"
CH = ["anima_clear", "anima-line", "anima-real", "hd_3_clear", "hd_3_curvy",
      "hd_3_gloss", "hd_3_soft", "silver", "silver-hd"]

from tools._split_identity import split  # noqa: E402

DATA = [
    ("cantarella", "坎特蕾拉", "cantarella (wuthering waves)",
     "wuthering waves, 1girl, purple hair, very long hair, hair between eyes, "
     "blue eyes, facial mark, mark under eye white dress, side slit, headdress, "
     "earrings, jewelry, single thighhigh, white thighhighs"),
    ("verina", "维里奈", "verina (wuthering waves)",
     "wuthering waves, 1girl, blonde hair, very long hair, twintails, "
     "short bangs, green eyes, tacet mark (wuthering waves) qixiong ruqun, "
     "hanfu, chinese clothes, see-through clothes, shorts under dress, "
     "bare shoulders, green hairband, black choker, white fingerless gloves, "
     "hair ornament"),
    ("mornye", "莫宁", "mornye (wuthering waves)",
     "wuthering waves, 1girl, very long hair, hair intakes, hair over one eye, "
     "red eyes, colored eyelashes, halo, see-through body, transparent legs "
     "purple dress, gradient dress, gradient clothes, white jacket, id card"),
    ("hsin", "心月狐", "hsin (wuthering waves)",
     "wuthering waves, 1girl, white hair, long hair, orange eyes, fox ears, "
     "fox girl, animal ears, chest tattoo, forehead tattoo, tattoo, "
     "long fingernails, mature female bare shoulders, hair ornament"),
    ("aemeath", "爱弥斯", "emeath (wuthering waves)",
     "wuthering waves, 1girl, pink hair, orange eyes, long hair, hair intakes, "
     "star-shaped pupils, symbol-shaped pupils, star (symbol), white leotard, "
     "strapless leotard, strapless, highleg leotard, highleg, white skirt, "
     "showgirl skirt, skirt, white gloves, elbow gloves, gloves, bare shoulders, "
     "hair ornament"),
]


def main():
    doc = json.load(open(INDEX, encoding="utf-8"))
    cs = doc.get("characters") or []
    by_key = {c.get("key"): c for c in cs}
    added = 0
    for key, name, trigger, tags in DATA:
        keep, drop = split(tags)
        ent = {
            "key": key, "name": name, "category": "鸣潮",
            "file": PACK, "trigger": trigger,
            "identity": keep, "wardrobe": drop, "strength": 0.8,
            "aliases": [key, name, trigger],
            "channels": CH, "source": "wuwa-all-anima-v2",
        }
        if key in by_key:
            # 已有专用 LoRA 的（爱弥斯），不动它，只记下包里也能出
            by_key[key]["also_in_pack"] = PACK
            print("跳过（已有专用 LoRA）:", name)
            continue
        cs.append(ent)
        added += 1
        print("新增:", name, "| identity:", keep[:70])
    doc["characters"] = cs
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("总条目:", len(cs), "| 本次新增:", added)


if __name__ == "__main__":
    main()
