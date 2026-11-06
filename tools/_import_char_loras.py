# -*- coding: utf-8 -*-
"""把下载的角色 LoRA 按**底模架构**分到各渠道，并写进角色库。

    python tools/_import_char_loras.py            # 只看方案，不落盘
    python tools/_import_char_loras.py --apply    # 复制文件 + 写索引

归属靠索引里的 `channels`：同一个 key 可以有多条，`char_loras.entry_by_key`
按当前渠道挑。所以「anima 的千夏」和「SDXL 的千夏」各在各的渠道里生效。

identity 是手工写的：`ss_tag_frequency` 自动提取在三种情况下会串味——
一个文件装多个角色（妄想天使三小只）、训练集混了 reg 图（晴雅）、
moss 用自然语言 caption 而不是 tag。自动值只当缺省兜底。
"""
import json
import os
import shutil
import struct
import sys

SRC = r"D:\Users\gjl\Downloads\娜娜莉（异环）"
LORAS = r"D:\AI\ComfyUI_v037\models\loras"
INDEX = r"D:\AI\agent_my_test\config\char_loras.json"

A = ["anima_clear", "anima-line", "anima-real", "hd_3_clear", "hd_3_curvy",
     "hd_3_gloss", "hd_3_soft", "silver", "silver-hd"]
XL = ["miao", "cunny", "jank", "nffa"]
SD15 = ["image_gen_v1", "image_gen_v1_hires", "nsfw_pose_gen"]
MS = ["moss", "moss-light", "moss-qunyou", "moss50", "moss8", "moss_page"]

# src, key, name, category, 目标相对路径, trigger, channels, identity
PLAN = [
    ("娜娜莉（异环）.safetensors", "nanally-v2", "娜娜莉 v2", "异环",
     r"anima\异环\nte-nanally-v2.safetensors",
     "nanally (neverness to everness)", A,
     "red hair, ahoge, blunt bangs, red eyes, x hair ornament, black bow, "
     "cat ears, armband, long sleeves, school uniform"),

    ("月雪宫子_(Blue_Archive)-Blue_Archive-variant-10506-anima-v1.safetensors",
     "miyako", "月雪宫子", "蔚蓝档案",
     r"anima\蔚蓝档案\ba-miyako.safetensors",
     "miyako (blue archive)", A,
     "halo, blue halo, long hair, purple eyes, grey hair, blue archive, "
     "1girl, solo"),

    ("Gemini娘-Anima-v1.0.safetensors", "gemini", "Gemini娘", "AI拟人",
     r"anima\AI拟人\gemini-anima-v1.safetensors", "gemini_ai", A,
     "blue hair, purple hair, multicolored hair, gradient hair, long hair, "
     "hair bun, ahoge, heterochromia, cat ears, cat tail, detached sleeves"),

    ("Gemini娘-moss-v1.safetensors", "gemini-moss", "Gemini娘·moss", "AI拟人",
     r"moss\gemini-moss-v1.safetensors", "gemini musume", MS,
     "very long hair, blue hair, purple hair, gradient hair, two-tone hair, "
     "hair bun, star hair ornament, heterochromia, red eyes, yellow eyes"),

    ("【鸣潮-Wuthering Waves】 今汐-Jinxi.safetensors", "jinhsi", "今汐", "鸣潮",
     r"sdxl\wuwa-jinhsi.safetensors", "jinhsi (wuthering waves)", XL,
     "long hair, bangs, twintails, grey eyes, hair ornament, jewelry, "
     "earrings, very long hair"),

    ("妄想天使三小只V4-000007.safetensors", "aria", "爱芮", "绝区零",
     r"sdxl\zzz-wingxiang-v4.safetensors", "aria (zenless zone zero)", XL,
     "pink hair, multicolored hair, wings, pink gloves, white dress, "
     "black thighhighs, detached sleeves, fingerless gloves"),

    ("妄想天使三小只V4-000007.safetensors", "chinatsu", "绝区零·千夏", "绝区零",
     r"sdxl\zzz-wingxiang-v4.safetensors", "sunna (zenless zone zero)", XL,
     "necktie, red necktie, white shirt, hair ornament, short sleeves, bag, "
     "shirt"),

    ("妄想天使三小只V4-000007.safetensors", "nangong", "南宫羽", "绝区零",
     r"sdxl\zzz-wingxiang-v4.safetensors", "nangong yu", XL,
     "multicolored hair, green hair bow, twintails, halo, bare shoulders, "
     "thighhighs"),

    ("抚顺（碧蓝航线）.safetensors", "fushun", "抚顺", "碧蓝航线",
     r"sdxl\azur-fushun.safetensors", "shunfu", XL,
     "twintails, ahoge, hair ornament, long sleeves, gloves, detached sleeves"),

    ("洛熙（终末地）.safetensors", "rossi", "洛茜", "终末地",
     r"sdxl\endfield-rossi.safetensors", "rossi (arknights)", XL,
     "animal ears, gloves, ears through hood, fingerless gloves, black gloves, "
     "hood"),

    ("洛天依  Luotianyi (SDXL).safetensors", "luotianyi", "洛天依", "Vocaloid",
     r"sdxl\vocaloid-luotianyi.safetensors", "luotianyi", XL,
     "green eyes, long hair, twintails, hair rings, hair ornament"),

    ("洛天依 V4 版-博歌乐中国（Luo Tianyi V4 Version-Vocaloid China）.safetensors",
     "luotianyi-v4", "洛天依 V4", "Vocaloid",
     r"sdxl\vocaloid-luotianyi-v4.safetensors", "luo tianyi", XL,
     "grey hair, very long hair, twintails, nail polish, skirt, pleated skirt"),

    ("【鸣潮-Wuthering Waves】折枝-SD1.5.safetensors", "zhezhi", "折枝", "鸣潮",
     r"sd15\wuwa-zhezhi.safetensors", "zhezhi", SD15,
     "black hair, long hair, bangs, earrings, jewelry, glasses, "
     "white pantyhose, dress"),

    ("保卫晴雅.safetensors", "qingya-bw", "晴雅·保卫", "晴雅",
     r"sd15\qingya-bw.safetensors", "cfqingyabw", SD15,
     "twintails, blue eyes, elbow gloves, pantyhose, crop top, belt, skirt"),

    ("潜伏晴雅.safetensors", "qingya-qf", "晴雅·潜伏", "晴雅",
     r"sd15\qingya-qf.safetensors", "cfqingyaqf", SD15,
     "twin braids, fingerless gloves, brown hair, pink skirt"),

    ("王者晴雅保卫.safetensors", "qingya-wz-bw", "王者晴雅·保卫", "晴雅",
     r"sd15\qingya-wz-bw.safetensors", "cfwangzheqingyabw", SD15,
     "blue eyes, white hair, elbow gloves, pantyhose, belt, bare shoulders"),

    ("王者晴雅潜伏.safetensors", "qingya-wz-qf", "王者晴雅·潜伏", "晴雅",
     r"sd15\qingya-wz-qf.safetensors", "cfwangzheqingyaqf", SD15,
     "blonde hair, gun, handgun, weapon, holster"),

    ("拉菲2（碧蓝航线）.safetensors", "laffey", "拉菲II", "碧蓝航线",
     r"sd15\azur-laffey-ii.safetensors", "laffey ii (azur lane)", SD15,
     "laffey (azur lane), laffey ii (azur lane), twintails, red eyes, "
     "white hair, long hair, azur lane"),

    ("雪风（碧蓝航线）.safetensors", "yukikaze", "雪风", "碧蓝航线",
     r"sd15\azur-yukikaze.safetensors", "yukikaze", SD15,
     "animal ears, cat ears, long hair, bow, hair bow, white hair, azur lane"),
]

SKIP = {
    "ai娘合集.safetensors": "画风包（aigirls），不是具体角色；进库会跟点名抢判定",
    "鸣潮全角色 wuwa all anima.safetensors":
        "与现有 anima\\鸣潮\\wuwa-all-anima-v2.safetensors 同一套合集，已在库里",
}

DROP = {
    "1girl", "2girls", "3girls", "solo", "looking at viewer", "simple background",
    "white background", "grey background", "full body", "upper body", "cowboy shot",
    "close-up", "portrait", "standing", "sitting", "lying", "walking", "smile",
    "closed mouth", "open mouth", "blush", "highres", "absurdres", "best quality",
    "masterpiece", "faceless", "nude", "nipples", "breasts", "completely nude",
    "pussy", "navel", "arms up", "from side", "from above", "from below",
    "straight-on", "outdoors", "indoors", "day", "night", "sky", "water",
}
KEEP_HINT = ("hair", "eyes", "ear", "ears", "tail", "halo", "horn", "ornament",
             "bow", "ribbon", "dress", "skirt", "uniform", "jacket", "coat",
             "glove", "sock", "thighhigh", "boots", "jewelry", "choker", "hat",
             "headwear", "eyewear", "glasses", "armband", "mark", "veil", "cape",
             "sleeve", "necktie", "badge", "belt", "weapon")


def meta(path):
    with open(path, "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        return json.loads(f.read(n)).get("__metadata__") or {}


def identity_of(path):
    """兜底用：从 ss_tag_frequency 提外观 tag。PLAN 里写了 identity 就不调用。"""
    m = meta(path)
    counts = {}
    try:
        for tags in json.loads(m.get("ss_tag_frequency") or "{}").values():
            for t, c in tags.items():
                t = t.replace("_", " ").strip().lower()
                if t and t not in DROP:
                    counts[t] = counts.get(t, 0) + c
    except Exception:                                        # noqa: BLE001
        pass
    hits = [(t, c) for t, c in counts.items() if any(h in t for h in KEEP_HINT)]
    hits.sort(key=lambda x: -x[1])
    return ", ".join(t for t, _ in hits[:8])


def main():
    apply = "--apply" in sys.argv
    idx = json.load(open(INDEX, encoding="utf-8"))
    chars = idx["characters"]
    before = len(chars)
    added, skipped = [], []

    for src, key, name, cat, rel, trigger, chans, ident in PLAN:
        sp = os.path.join(SRC, src)
        if not os.path.isfile(sp):
            print("!! 源文件缺失:", src)
            continue
        # 同一个 key + 同一组渠道 = 已导入过，跳过
        if any(c.get("key") == key and c.get("channels") == chans for c in chars):
            skipped.append("%s（同渠道已有）" % key)
            continue
        if not ident:
            ident = identity_of(sp)
        entry = {
            "key": key, "name": name, "category": cat,
            "file": rel, "trigger": trigger,
            "identity": ident,
            "strength": 0.8,
            "aliases": [trigger, name],
            "channels": chans,
        }
        if apply:
            dst = os.path.join(LORAS, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not (os.path.isfile(dst)
                    and os.path.getsize(dst) == os.path.getsize(sp)):
                shutil.copy2(sp, dst)
            if not any(c.get("key") == key and c.get("file") == rel
                       and c.get("channels") == chans for c in chars):
                chars.append(entry)
        added.append(entry)
        print("%-14s %-12s %-22s %s" % (key, name, rel, ident[:56]))

    print("\n--- 不进库")
    for k, why in SKIP.items():
        print("    %-42s %s" % (k, why))
    for s in skipped:
        print("    " + s)

    if apply:
        with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
            json.dump(idx, f, ensure_ascii=False, indent=2)
        print("\n索引：%d → %d 条（新增 %d）" % (before, len(chars),
                                              len(chars) - before))
    else:
        print("\n[dry-run] 加 --apply 才复制文件、写索引")


if __name__ == "__main__":
    main()
