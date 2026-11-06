# -*- coding: utf-8 -*-
"""把 LoRA 的 .txt 拆成「角色特征」和「衣服/配件」两段（2026-11-06）。

问题：每个 .txt 是作者把**外观 + 衣服 + 随身物**一口气写完的，比如琳奈那份
里有 `white shirt, pleated skirt, black belt, harness, headphones around neck,
badge, id card`。整段拼进提示词，用户说「换 JK」也压不过它——衣服被写死了。

做法：按词表把 tag 分成两类。
  · **特征**（保留）：头发 / 眼睛 / 脸 / 体型 / 非人特征（光环、翼、角、耳）
  · **衣服与配件**（挪出去，存成 `wardrobe`）：衣物 / 鞋袜 / 首饰 / 手持物
默认只拼「特征」；用户明确要穿什么时，由生图那边自己加，不再和写死的衣服打架。

用法：py -3 tools/_split_identity.py            # dry-run，看几个样本
      py -3 tools/_split_identity.py --apply    # 写回索引并重建向量
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, "config", "char_loras.json")
APPLY = "--apply" in sys.argv

# 衣服 / 鞋袜 / 首饰 / 随身物：这些**不进身份**。
# 用**子串**匹配（不加前 \b）：`miniskirt` / `thighhighs` / `headdress`
# 这类带前缀的词才不会漏。
CLOTH = re.compile(
    r"(shirt|blouse|skirt|dress|gown|uniform|outfit|clothes|jacket|coat|"
    r"cape|cloak|hoodie|sweater|cardigan|vest|apron|overalls|pants|trousers|"
    r"shorts|jeans|leggings|tights|swimsuit|bikini|leotard|bodysuit|lingerie|"
    r"bra\b|panties|underwear|socks?\b|stockings?|thighhighs?|pantyhose|"
    r"legwear|boots?\b|shoes?\b|sandals?|heels?\b|sneakers?|slippers?|"
    r"hat\b|cap\b|beret|headdress|headdress|veil\b|crown\b|tiara|necklace|"
    r"choker|bracelet|bangle|ring\b|harness|belt\b|gloves?\b|armband|"
    r"wristband|headphones?|badge|id card|bag\b|backpack|randoseru|umbrella|"
    r"weapon|sword|staff\b|mask\b|earrings?|jewelry|necktie|tie\b|scarf|"
    r"bowtie|sash\b|armor|straps?\b|suspenders?|sleeves?|collar|zipper|"
    r"headwear|kneehighs?|footwear|barefoot|footwraps?|"
    r"buttons?\b|pockets?\b|frills?\b|lace\b|coat|shoes)", re.I)

# 明确属于**身份**的（哪怕命中上面的词也保留）
KEEP = re.compile(
    r"hair|eyes?|pupils?|bangs|sidelocks|ahoge|braid|ponytail|updo|bun\b|"
    r"halo|wings?|horns?|ears?|fangs?|tail\b|scales?|skin|breasts?|cleavage|"
    r"navel|thighs?|mole|freckles?|blush", re.I)


def split(identity):
    keep, wardrobe = [], []
    for tok in re.split(r"[,\n]", str(identity or "")):
        t = tok.strip(" .;，")
        if not t:
            continue
        if CLOTH.search(t) and not KEEP.search(t):
            wardrobe.append(t)
        else:
            keep.append(t)
    return ", ".join(keep), ", ".join(wardrobe)


def main():
    d = json.load(open(INDEX, encoding="utf-8"))
    cs = d.get("characters") or []
    samples = [c for c in cs if c.get("key") in
               ("linnai", "aemeath", "chinatsu", "hutao")]
    for c in samples:
        keep, drop = split(c.get("identity"))
        print("=" * 60)
        print(c.get("name"), "(%s)" % c.get("key"))
        print("  保留:", keep[:200])
        print("  挪出:", drop[:160])
    if not APPLY:
        print("\n（dry-run，加 --apply 才写回）")
        return
    for c in cs:
        keep, drop = split(c.get("identity"))
        c["identity"] = keep
        c["wardrobe"] = drop
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    print("\n已写回 %d 条；向量索引会在下次检索时自动重建" % len(cs))


if __name__ == "__main__":
    main()
