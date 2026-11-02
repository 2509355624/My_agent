"""清理和泉纱雾外观串里与成年设定冲突的词（一次性脚本）。

原串带着 `loli`、`petite`、`small breasts`、`fragile appearance`——人物设定
2026-11-02 已改成 20 岁，外观串不改的话，出图提示词里仍然塞着未成年体型词，
和人设直接打架，而且接进亲密玩法时是明确的内容红线。

顺带去掉三个画师名 tag（`ask` / `ciloranko` / `sho`）和 `tianliang_*`：
按 MOSS/SD 的实测，未收录的画师名不产生可辨差异，纯占预算。

    python tools/_fix_card_sagiri_appearance.py
"""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "agents", "qq", "characters.json")

CID = "izumi_sagiri"

APPEARANCE = (
    "masterpiece, best quality, ultra-detailed, absurdres, sfw, "
    "1girl, solo, silver hair, long hair, waist-length hair, blue eyes, pale skin, "
    "slender adult figure, bright shy expression, pink hair ribbons, twin tails, "
    "blush, natural adult proportions"
)


def main():
    with open(PATH, "r", encoding="utf-8") as f:
        cards = json.load(f)
    n = 0
    for c in cards:
        if c.get("id") == CID:
            c["appearancePrompt"] = APPEARANCE
            n += 1
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cards, f, ensure_ascii=False, indent=2)
    print("updated %d cards" % n)


if __name__ == "__main__":
    main()
