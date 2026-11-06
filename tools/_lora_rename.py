# -*- coding: utf-8 -*-
"""P1-a：LoRA 规范化命名 + 生成角色库索引（2026-11-06）。

命名规则：`<游戏>-<角色 key>.safetensors`，全 ASCII、小写、连字符。
中文名不进文件名（跨工具处理编码麻烦），但完整保留在索引的 `name` 里。

用法：
    py -3 tools/_lora_rename.py            # dry-run：只打印，不动文件
    py -3 tools/_lora_rename.py --apply    # 真改：重命名 + 写索引

改名时会连带改同名的 .txt / .png。索引写到 `config/char_loras.json`，
`channels` 填当前放行的 8 个 anima 渠道。
"""
import io
import json
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = r"D:\AI\ComfyUI_v037\models\loras"
LIB = os.path.join(ROOT, "anima")
INDEX = os.path.join(BASE, "config", "char_loras.json")
APPLY = "--apply" in sys.argv

# 目录 → 游戏前缀
GAME = {"原神": "genshin", "异环": "nte", "星穹铁道": "hsr",
        "终末地": "endfield", "鸣潮": "wuthering"}

CHANNELS = ["anima-line", "anima-real", "hd_3_clear", "hd_3_curvy",
            "hd_3_gloss", "hd_3_soft", "silver", "silver-hd"]

# 旧文件名（不含扩展名）→ (游戏前缀, 新 key, 中文名)
MAP = {
    # 原神
    "八重神子": ("genshin", "yae-miko", "八重神子"),
    "哥伦比亚": ("genshin", "columbina", "哥伦比亚"),
    "奥黛塔": ("genshin", "odette", "奥黛塔"),
    "尼可": ("genshin", "nico", "尼可"),
    "桑多涅": ("genshin", "sandrone", "桑多涅"),
    "梦见月瑞希": ("genshin", "mizuki", "梦见月瑞希"),
    "爱可菲": ("genshin", "escoffier", "爱可菲"),
    "珊瑚宫心海": ("genshin", "kokomi", "珊瑚宫心海"),
    "纳西妲": ("genshin", "nahida", "纳西妲"),
    "胡桃": ("genshin", "hutao", "胡桃"),
    "芙宁娜": ("genshin", "furina", "芙宁娜"),
    "若娜瓦": ("genshin", "ronova", "若娜瓦"),
    "茜特菈莉": ("genshin", "citlali", "茜特菈莉"),
    "莉奈娅": ("genshin", "linaya", "莉奈娅"),
    # 异环
    "Anima_曳尔沐": ("nte", "elyms", "曳尔沐"),
    "妮夏": ("nte", "nitsa", "妮夏"),
    "娜娜莉": ("nte", "nanally", "娜娜莉"),
    "安魂曲": ("nte", "lacrimosa", "安魂曲"),
    "小吱": ("nte", "chiz", "小吱"),
    "珀塞特": ("nte", "perset", "珀塞特"),
    # 星穹铁道
    "三月七": ("hsr", "march7", "三月七"),
    "云璃": ("hsr", "yunli", "云璃"),
    "克拉拉": ("hsr", "clara", "克拉拉"),
    "刻律德菈": ("hsr", "cerydra", "刻律德菈"),
    "昔涟": ("hsr", "cyrene", "昔涟"),
    "流萤": ("hsr", "firefly", "流萤"),
    "海瑟音": ("hsr", "hysilens", "海瑟音"),
    "火花": ("hsr", "sparxie", "火花"),
    "火花皮肤": ("hsr", "sparxie-candy", "火花·糖果皮肤"),
    "爻光": ("hsr", "yaoguang", "爻光"),
    "知更鸟-晴歌": ("hsr", "robin-serenade", "知更鸟·晴歌"),
    "知更鸟": ("hsr", "robin", "知更鸟"),
    "符玄": ("hsr", "fuxuan", "符玄"),
    "绯英": ("hsr", "evanescia", "绯英"),
    "花火": ("hsr", "sparkle", "花火"),
    "藿藿": ("hsr", "huohuo", "藿藿"),
    "远坂凛": ("hsr", "rin", "远坂凛"),
    "遐蝶": ("hsr", "castorice", "遐蝶"),
    "遐蝶皮肤": ("hsr", "castorice-dreamy", "遐蝶·梦蝶皮肤"),
    "银狼": ("hsr", "silverwolf", "银狼"),
    "青雀": ("hsr", "qingque", "青雀"),
    "风堇": ("hsr", "hyacine", "风堇"),
    "黑塔": ("hsr", "herta", "黑塔"),
    # 终末地
    "佩丽卡": ("endfield", "perlica", "佩丽卡"),
    "洛茜": ("endfield", "rossi", "洛茜"),
    "祀": ("endfield", "si", "祀"),
    "莉诺": ("endfield", "lino", "莉诺"),
    # 鸣潮
    "今汐皮肤": ("wuthering", "jinhsi-peach", "今汐·桃花皮肤"),
    "千咲": ("wuthering", "chisa", "千咲"),
    "卡提希娅": ("wuthering", "cartethyia", "卡提希娅"),
    "守岸人": ("wuthering", "shorekeeper", "守岸人"),
    "清宵": ("wuthering", "qingxiao", "清宵"),
    "爱弥斯": ("wuthering", "aemeath", "爱弥斯"),
    "珂莱塔": ("wuthering", "carlotta", "珂莱塔"),
    "琳奈": ("wuthering", "linnai", "琳奈"),
    "秧秧玄翎": ("wuthering", "yangyang", "秧秧·玄翎"),
    "穗穗": ("wuthering", "suisui", "穗穗"),
    "菲比": ("wuthering", "phoebe", "菲比"),
    "达妮娅": ("wuthering", "daniya", "达妮娅"),
    # 根目录
    "sunna-anima_20260216_2230": ("zzz", "chinatsu", "绝区零·千夏"),
}


def clean_trigger(line):
    s = str(line or "").strip().strip('"').strip("'")
    for junk in (", 1girl", ",1girl", "，1girl"):
        s = s.replace(junk, "")
    return s.strip(" ,，")


def read_txt(p):
    """→ (触发词, 外观 tag 串)。没有 txt 就给空。"""
    if not os.path.isfile(p):
        return "", ""
    try:
        lines = [x.strip() for x in
                 open(p, encoding="utf-8").read().splitlines() if x.strip()]
    except Exception:                                  # noqa: BLE001
        return "", ""
    if not lines:
        return "", ""
    trig = clean_trigger(lines[0])
    body = " ".join(lines[1:]).strip(" ,，")
    return trig, body


def walk():
    for root, _d, files in os.walk(LIB):
        for fn in files:
            if fn.lower().endswith(".safetensors"):
                yield os.path.join(root, fn)


def main():
    files = list(walk())
    rel_dir = os.path.relpath
    rows, unmapped = [], []
    for p in files:
        stem = os.path.splitext(os.path.basename(p))[0]
        sub = rel_dir(os.path.dirname(p), LIB)
        game_dir = "" if sub == "." else sub
        info = MAP.get(stem)
        if not info:
            unmapped.append((game_dir, stem))
            continue
        prefix, key, cn = info
        if game_dir and GAME.get(game_dir) != prefix:
            print("⚠️ 目录与前缀不符：%s/%s → %s" % (game_dir, stem, prefix))
        new_stem = "%s-%s" % (prefix, key)
        trig, body = read_txt(os.path.join(os.path.dirname(p), stem + ".txt"))
        rows.append({
            "old": p, "dir": os.path.dirname(p), "old_stem": stem,
            "new_stem": new_stem, "key": key, "name": cn,
            "category": game_dir or "zzz", "trigger": trig,
            "identity": body, "has_txt": bool(trig),
        })

    if unmapped:
        print("❌ 有文件没配映射，先补 MAP：")
        for d, s in unmapped:
            print("   [%s] %s" % (d or "根目录", s))
        if APPLY:
            return
    print("待处理 %d 个文件\n" % len(rows))
    for r in rows:
        print("  %-46s → %-30s %s"
              % (os.path.basename(r["old"])[:46],
                 r["new_stem"] + ".safetensors",
                 ("触发词=" + r["trigger"]) if r["has_txt"] else "⚠️ 无触发词"))

    if not APPLY:
        print("\n（dry-run，加 --apply 才真改）")
        return

    # 1) 改名（safetensors + 同名 txt/png），并把映射写进日志，方便反悔
    moved = 0
    log = []
    for r in rows:
        for ext in (".safetensors", ".txt", ".png"):
            src = os.path.join(r["dir"], r["old_stem"] + ext)
            dst = os.path.join(r["dir"], r["new_stem"] + ext)
            if os.path.isfile(src) and src != dst:
                if os.path.exists(dst):
                    print("  跳过（目标已存在）：", dst)
                    continue
                os.rename(src, dst)
                log.append({"from": src, "to": dst})
                if ext == ".safetensors":
                    moved += 1
    print("\n改名完成：%d 个" % moved)
    with open(os.path.join(BASE, "state", "lora_rename_log.json"), "w",
              encoding="utf-8", newline="\n") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print("改名日志：state/lora_rename_log.json（可据此还原）")

    # 2) 写索引；老索引里的手工字段（千夏的外观是照着 Civitai 写的，
    #    没有 .txt 可读）要保留下来，不能被空值覆盖
    old = {}
    try:
        for c in (json.load(open(INDEX, encoding="utf-8"))
                  .get("characters") or []):
            old[str(c.get("key") or "")] = c
    except Exception:                                  # noqa: BLE001
        pass
    # 千夏没有 .txt，触发词按 Civitai 上写的补
    FALLBACK = {"chinatsu": ("sunnazzz", "")}
    chars = []
    for r in sorted(rows, key=lambda x: (x["category"], x["key"])):
        prev = old.get(r["key"]) or {}
        trig = r["trigger"] or FALLBACK.get(r["key"], ("", ""))[0] \
            or prev.get("trigger") or ""
        ident = r["identity"] or prev.get("identity") or ""
        rel = os.path.relpath(os.path.join(r["dir"], r["new_stem"]
                                           + ".safetensors"), ROOT)
        aliases = [trig, r["key"], r["name"]]
        for a in (prev.get("aliases") or []):
            aliases.append(a)
        if trig and trig.lower() != r["key"]:
            aliases.append(trig.lower())
        seen, al = set(), []
        for a in aliases:
            a = str(a or "").strip()
            if a and a.lower() not in seen:
                seen.add(a.lower())
                al.append(a)
        chars.append({
            "key": r["key"], "name": r["name"], "category": r["category"],
            "file": rel, "trigger": trig, "identity": ident,
            "strength": prev.get("strength") or 0.8,
            "aliases": al, "channels": CHANNELS,
        })
    doc = {"_doc": "角色库（2026-11-06 规范化）：file 相对 ComfyUI loras 目录；"
                   "trigger/identity 取自 lora 同名的 .txt；channels 是允许挂载"
                   "的渠道。检索由角色库 AI 走向量召回 + 大模型判定，"
                   "不做别名硬匹配。",
           "characters": chars}
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("索引已写：%s（%d 条）" % (INDEX, len(chars)))
    miss = [c["key"] for c in chars if not c["identity"]]
    if miss:
        print("⚠️ 没有外观 tag 的：", miss)


if __name__ == "__main__":
    main()
