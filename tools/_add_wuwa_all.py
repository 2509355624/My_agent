# -*- coding: utf-8 -*-
"""把「鸣潮全角色」那个多角色 LoRA 拆成索引条目（2026-11-06）。

一个 176MB 的 safetensors 里装了 47 个角色，每个角色靠一句触发词唤出：

    cantarella (wuthering waves), wuthering waves, 1girl, purple hair, ...

所以索引里每一条都指向**同一个文件**，只是 trigger / identity 不同。
`char_loras.patch()` 按 file 插节点、按 trigger 拼提示词，天然支持这种写法。

用法：py -3 tools/_add_wuwa_all.py [--apply]
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                              errors="replace")

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
INDEX = os.path.join(BASE, "config", "char_loras.json")
PACK = "anima\\鸣潮\\wuwa-all-anima-v2.safetensors"
APPLY = "--apply" in sys.argv

# 触发词里给的中文名（认识的先写死，剩下的用英文名占位）
CN = {"cantarella": "坎特蕾拉", "verina": "维里奈", "mornye": "莫宁",
      "hsin": "心月狐", "cartethyia": "卡提希娅", "carlotta": "珂莱塔",
      "phoebe": "菲比", "lynae": "琳奈", "chisa": "千咲", "qingxiao": "清宵",
      "shorekeeper": "守岸人", "suisui": "穗穗", "yangyang": "秧秧",
      "jinhsi": "今汐", "yinlin": "吟霖", "calcharo": "卡卡罗",
      "encore": "安可", "chixia": "炽霞", "danjin": "丹瑾", "baizhi": "白芷",
      "zhezhi": "折枝", "camellya": "椿", "roccia": "洛可可", "zani": "赞妮",
      "ciaccona": "夏空", "lupa": "露帕", "lumi": "灯灯", "youhu": "釉瑚",
      "jianxin": "鉴心", "taoqi": "桃祈", "yuanwu": "渊武", "emeath": "爱弥斯",
      "aemeath": "爱弥斯", "augusta": "奥古斯塔", "buling": "卜灵",
      "fleurdelys": "菲蕾德丽丝", "phrolova": "弗洛洛", "galbrena": "加布蕾娜",
      "jingran": "景然", "nivora": "妮沃拉", "anhua": "岸花", "suoming": "朔明",
      "sigrika": "希格莉卡", "iuno": "尤诺", "hiyuki": "日雪", "denia": "德妮娅",
      "lucilla": "露西拉"}


def fetch_trained():
    """从 Civitai API 取这个模型版本的 trainedWords（每段一个角色）。"""
    import urllib.request
    tok = os.getenv("CIVITAI_TOKEN", "")
    req = urllib.request.Request(
        "https://civitai.com/api/v1/models/2948949",
        headers={"Authorization": "Bearer " + tok} if tok else {})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    return d["modelVersions"][0]["trainedWords"] or []


def split(identity):
    """复用拆身份那套词表。"""
    from tools._split_identity import split as sp
    return sp(identity)


def main():
    words = fetch_trained()
    print("trainedWords 段数:", len(words))
    doc = json.load(open(INDEX, encoding="utf-8"))
    cs = doc.get("characters") or []
    have = {c.get("key") for c in cs}
    added = []
    for w in words:
        m = re.match(r"\s*([a-z_ ]+?) \(wuthering waves\)(.*)$", w, re.I)
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        if key == "emeath":
            key = "aemeath"           # API 少打了个 a
        if key in have:
            continue
        body = m.group(2).strip(" ,")
        keep, drop = split(body)
        added.append({
            "key": key, "name": CN.get(key, key), "category": "鸣潮",
            "file": PACK,
            "trigger": "%s (wuthering waves)" % m.group(1).strip(),
            "identity": keep, "wardrobe": drop, "strength": 0.8,
            "aliases": [key, CN.get(key, key),
                        "%s (wuthering waves)" % m.group(1).strip()],
            "channels": ["anima_clear", "anima-line", "anima-real",
                         "hd_3_clear", "hd_3_curvy", "hd_3_gloss", "hd_3_soft",
                         "silver", "silver-hd"],
        })
    print("新增条目:", len(added))
    for a in added[:6]:
        print("  %-12s %-8s %s" % (a["key"], a["name"], a["identity"][:70]))
    if not APPLY:
        print("\n（dry-run，加 --apply 才写回）")
        return
    cs.extend(added)
    doc["characters"] = cs
    with open(INDEX, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    print("已写回，总条目:", len(cs))


if __name__ == "__main__":
    main()
