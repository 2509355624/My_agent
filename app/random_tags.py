# -*- coding: utf-8 -*-
"""随机生图口令的策展 tag 池（2026-10-05 新增）。

「随机萝莉 / 随机兽耳 / 随机女仆 / 今日老婆」四个口令的弹药库。原则
（与 skills/anima-tags 的铁律同源）：

- 每一个内容 tag 都必须能在 skills/anima-tags/data/tags.tsv（32.8 万条
  Danbooru 库）里查到——`validate()` 在测试里钉死「池子 ⊆ 库」。库本身
  无热度无筛选（裸抽会抓出猎奇词），所以池子是**策展**的，库只用来验
  存在性和反查中文名。
- 全部 safe 向：服装/姿势池只收干净词（2026-10-05 用户拍板）。
- 今日老婆的角色池：代码里放默认池，`agents/qq/waifu_pool.json` 存在则
  覆盖（用户私货名单，agents/ 已 gitignore）。每个角色带 look 外貌
  tag——角色名小模型可能不认，外貌 tag 才是出图保底（「神里绫华写出
  Shiori Sakura」实录教训）。
"""
import io
import json
import os
import random

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TSV = os.path.join(_ROOT, "skills", "anima-tags", "data", "tags.tsv")
WAIFU_POOL_PATH = os.path.join(_ROOT, "agents", "qq", "waifu_pool.json")

HAIR_COLOR = ["white_hair", "blonde_hair", "black_hair", "brown_hair",
              "blue_hair", "pink_hair", "purple_hair", "red_hair",
              "green_hair", "grey_hair", "orange_hair"]
HAIR_STYLE = ["long_hair", "short_hair", "twintails", "ponytail", "bob_cut",
              "hime_cut", "ahoge", "blunt_bangs", "hair_ornament",
              "hair_ribbon"]
EYES = ["blue_eyes", "red_eyes", "green_eyes", "yellow_eyes", "purple_eyes",
        "brown_eyes", "pink_eyes", "heterochromia"]
OUTFIT_CUTE = ["dress", "white_dress", "sailor_dress", "school_uniform",
               "skirt", "sweater", "hoodie", "kimono", "apron", "thighhighs",
               "zettai_ryouiki", "hair_flower", "ribbon", "bow", "hairband",
               "puffy_sleeves", "frills"]
POSE_SCENE = ["sitting", "standing", "waving", "smile", "open_mouth", "blush",
              "eating", "drinking", "outdoors", "garden", "classroom",
              "cherry_blossoms", "sunlight", "looking_at_viewer",
              "upper_body", "cowboy_shot"]
# 姿势/构图/场景/表情**分类抽样**（各抽 1）：混在一个池里裸抽会出
# 「sitting, standing」这种互斥组合（实测抓到）。
_EXPR = ["smile", "open_mouth", "blush"]
_POSE = ["sitting", "standing", "waving", "eating", "drinking"]
_COMPO = ["looking_at_viewer", "upper_body", "cowboy_shot"]
_SCENE = ["outdoors", "garden", "classroom", "cherry_blossoms", "sunlight"]

# 主题骨架：base 恒在，extras 随机挑一个，tail=True 时加尾巴（兽耳）。
THEMES = {
    "萝莉": {"base": ["1girl", "loli", "solo"]},
    "兽耳": {"base": ["1girl", "animal_ears", "animal_ear_fluff", "solo"],
             "extras": ("cat_ears", "fox_ears"), "tail": True},
    "女仆": {"base": ["1girl", "maid", "maid_apron", "maid_headdress",
                      "solo"],
             "extras": ("tray",)},
}


def _scene_tags(rng):
    """表情/姿势/构图/场景各抽 1（互斥组合防串）。"""
    return [rng.choice(_EXPR), rng.choice(_POSE), rng.choice(_COMPO),
            rng.choice(_SCENE)]


def _all_pool_tags():
    tags = set(HAIR_COLOR + HAIR_STYLE + EYES + OUTFIT_CUTE + POSE_SCENE)
    for t in THEMES.values():
        tags.update(t["base"])
        tags.update(t.get("extras") or ())
        if t.get("tail"):
            tags.add("tail")
    return tags


ALL_POOL_TAGS = _all_pool_tags()


def sample_prompt(theme, rng=None):
    """按主题抽一个完整提示词。纯本地零调用。"""
    rng = rng or random
    t = THEMES[theme]
    tags = list(t["base"])
    if t.get("extras"):
        tags.append(rng.choice(t["extras"]))
    if t.get("tail"):
        tags.append("tail")
    tags.append(rng.choice(HAIR_COLOR))
    tags.append(rng.choice(HAIR_STYLE))
    tags.append(rng.choice(EYES))
    tags += rng.sample(OUTFIT_CUTE, 2)
    tags += _scene_tags(rng)
    return ", ".join(tags)


# 默认「今日老婆」角色池：全部 tag 已在库内验过（2026-10-05）。改池子
# 优先改 agents/qq/waifu_pool.json，不用动代码；新角色的 look tag 记得
# 先去库里验存在（validate 只覆盖默认池）。
DEFAULT_WAIFU_POOL = [
    {"tag": "nahida_(genshin_impact)", "cn": "纳西妲",
     "look": ["white_hair", "green_eyes", "hair_between_eyes"]},
    {"tag": "klee_(genshin_impact)", "cn": "可莉",
     "look": ["blonde_hair", "red_eyes", "hat", "backpack"]},
    {"tag": "kafuu_chino", "cn": "香风智乃",
     "look": ["blue_hair", "blue_eyes", "rabbit_hair_ornament"]},
    {"tag": "hoto_cocoa", "cn": "保登心爱",
     "look": ["blue_hair", "blue_eyes"]},
    {"tag": "tedeza_rize", "cn": "天天座理世",
     "look": ["black_hair", "purple_eyes"]},
    {"tag": "kanna_kamui", "cn": "康娜",
     "look": ["white_hair", "pink_eyes", "dragon_horns", "dragon_tail"]},
    {"tag": "megumin", "cn": "惠惠",
     "look": ["brown_hair", "red_eyes", "witch_hat"]},
    {"tag": "illyasviel_von_einzbern", "cn": "伊莉雅",
     "look": ["white_hair", "red_eyes"]},
    {"tag": "izumi_sagiri", "cn": "和泉纱雾",
     "look": ["white_hair", "blue_eyes"]},
]


def _norm_pool(data):
    """清洗外部池：留有 tag 的条目，look 去空。坏文件回落默认池。"""
    entries = []
    for e in data:
        if isinstance(e, dict) and str(e.get("tag") or "").strip():
            entries.append({
                "tag": str(e["tag"]).strip(),
                "cn": str(e.get("cn") or e["tag"]).strip(),
                "look": [str(x).strip() for x in (e.get("look") or [])
                         if str(x).strip()],
            })
    return entries or DEFAULT_WAIFU_POOL


def load_waifu_pool():
    """读用户的私货池；不存在/写坏回落内置默认池。"""
    try:
        with io.open(WAIFU_POOL_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and data:
            return _norm_pool(data)
    except Exception:
        pass
    return DEFAULT_WAIFU_POOL


def draw_waifu(rng=None):
    """随机抽一个角色 → (中文名, prompt)。每人随机（2026-10-05 拍板）。"""
    rng = rng or random
    e = rng.choice(load_waifu_pool())
    tags = ([e["tag"]] + list(e["look"])
            + rng.sample(OUTFIT_CUTE, 2) + _scene_tags(rng))
    return e["cn"], ", ".join(tags)


def validate():
    """默认池 + 主题池 ⊆ 库。返回缺失 tag 列表（测试断言为空）。"""
    have = set()
    with io.open(_TSV, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if p:
                have.add(p[0])
    tags = set(ALL_POOL_TAGS)
    for e in DEFAULT_WAIFU_POOL:
        tags.add(e["tag"])
        tags.update(e["look"])
    return sorted(t for t in tags if t not in have)
