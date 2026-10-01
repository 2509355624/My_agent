"""把 3 个尺寸档 × 4 个画风展开成 12 个生图渠道。

## 为什么是「组合」而不是 12 份手工工作流

尺寸档（hd_fast / hd_2 / hd_3）之间只差三件事：放大倍率、一段采样器、二段
步数；画风（clear / soft / gloss / curvy）只差**两段各用哪块底模**。两者正交，
所以 12 份工作流 = 3 份尺寸骨架 × 4 组底模名字，没必要手抄。

**唯一改的就是 `UNETLoader(5)`（一段）和 `UNETLoader(20)`（二段）的
`unet_name`** —— 采样器、步数、CFG、放大倍率、画布、提示词模板全部照抄档位
骨架。这样以后用户调某个档位的旋钮，重跑本脚本就能同步到 4 个画风上。

## 用法

    python _make_hd_channels.py          # 先干跑，看要建什么
    python _make_hd_channels.py --write  # 真写

写之前会打印每个渠道的「一段 → 二段」，对着看一眼再 --write。
"""

import copy
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "skills")

REALSKIN = "miaomiaoRealskin_anima13.safetensors"
REALITY = "miaomiaoAnimeReality_ani11_3087842.safetensors"
HAREM = "miaomiaoHarem_anima16.safetensors"

# ── 画风 = 底模组合（一段 → 二段）────────────────────────
# 组合照抄 4 个常规渠道（anima_clear / soft / gloss / curvy），
# 那四个的画风就是这套组合跑出来的实测结果。
STYLES = [
    ("clear", "清透素净", REALSKIN, REALSKIN,
     "最柔和均匀、光最平、哑光、素净、对比最低",
     "清透 / 素净 / 自然 / 光别那么硬"),
    ("soft", "柔光哑光素肌", REALSKIN, REALITY,
     "柔光、哑光、皮肤细腻偏素、对比低、层次稍多",
     "柔一点 / 素 / 干净 / 温柔 / 层次多一点"),
    ("gloss", "冷调油光", REALITY, REALSKIN,
     "冷调偏蓝、油光高光明显、锐利、3D 感",
     "亮面 / 油光 / 高光 / 冷色 / 通透"),
    ("curvy", "丰腴强光影", HAREM, REALITY,
     "胸围明显更大、光影强烈、皮肤油亮、氛围浓",
     "丰满 / 大胸 / 身材好 / 光影强 / 氛围感"),
]

# ── 尺寸档：骨架取自现有哪一个渠道 + 参数实况（写进 skill.md 用）──
TIERS = [
    ("fast", "高清快档", "hd_fast", "1×（不放大）", "1024×1536",
     "er_sde", 10, "euler", 5),
    ("2", "高清二档", "hd_2", "1.3×", "1328×2000",
     "dpmpp_2m", 10, "euler", 5),
    ("3", "高清三档", "hd_3", "1.5×", "1536×2304",
     "er_sde", 10, "euler", 10),
]


def _load(name):
    raw = open(os.path.join(SKILLS, name, "workflow.json"),
               encoding="utf-8").read()
    return json.loads(raw.replace(': __SEED__', ': "__SEED__"'))


def _dump(path, wf):
    txt = json.dumps(wf, ensure_ascii=False, indent=2)
    # 写回裸占位符：load_workflow 靠 ": __SEED__" 认它（见 app/skills.py）
    txt = txt.replace(': "__SEED__"', ": __SEED__")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt + "\n")


SKILL_MD = """---
kind: 生图
---

# {chan} · {tier_cn} × {style_cn}

**{tier_cn}** 的尺寸，**{style_cn}** 的画风。

尺寸档管「出多大」，画风管「底模怎么搭」——本渠道是两者的交叉。

| | 本渠道 |
|---|---|
| 一段底模 | `{s1}` |
| 二段底模 | `{s2}` |
| 一段采样 | `{samp1}` · {st1} 步 · cfg 5 |
| 二段采样 | `{samp2}` · {st2} 步 · cfg 5 · denoise 0.25 |
| 放大 | `{scale}`（`nearest-exact`，夹在两段之间） |
| 画布 → 输出 | 1024×1536 → **{out}** |

## 画风：{style_cn}

{style_desc}。点词：{style_words}。

**画风 = 两段底模的组合**，跟同画风的常规渠道（`anima_{style}`）是同一套组合，
只是画布更大。四个画风：

| 画风 | 一段 → 二段 | 点词 |
|---|---|---|
| `clear` | Realskin → Realskin | 清透 / 素净 / 光别那么硬 |
| `soft` | Realskin → AnimeReality | 柔 / 素 / 干净 / 层次多一点 |
| `gloss` | AnimeReality → Realskin | 亮面 / 油光 / 冷色 |
| `curvy` | Harem → AnimeReality | 丰满 / 大胸 / 光影强 |

> 用户只说「{tier_short}」没说画风 → 走 `{tier}_clear`（默认画风，跟全局默认
> `anima_clear` 口径一致）。

## 尺寸：{tier_cn}

三档排序：`hd_fast_*`（1× → 1024×1536）< `hd_2_*`（1.3× → 1328×2000）
< `hd_3_*`（1.5× → 1536×2304）。

> ⚠️ 这 12 个渠道都比 4 个常规渠道**重**（画布 1024×1536 起步），显存吃紧时
> 优先走常规渠道。本机 6GB 显存。

## 提示词

节点 4 的模板是 `@kibro, __MULTI_PROMPTS__`。`@kibro` 是 kibro LoRA 的触发词，
**不能丢**。负向在节点 8，未改动。

## 怎么来的（别手改 workflow.json）

本渠道由 `_make_hd_channels.py` 从**尺寸骨架** + **画风底模组合**生成：

```bat
cd /d D:\\AI\\My_agent
python _make_hd_channels.py --write
```

骨架来自 ComfyUI 里那份 `{src}` 工作流（用
`_sync_comfy_workflow.py "{src}" <渠道> --prefix "@kibro, "` 重导）。
**改尺寸参数改骨架、改画风改底模，然后重跑脚本**——手改某一份会让 12 个渠道
之间悄悄不一致。
"""


def main(write):
    made = []
    for tkey, tier_cn, src, scale, out, samp1, st1, samp2, st2 in TIERS:
        base = _load(src)
        for skey, style_cn, s1, s2, style_desc, style_words in STYLES:
            chan = "hd_%s_%s" % (tkey, skey)
            wf = copy.deepcopy(base)
            wf["5"]["inputs"]["unet_name"] = s1
            wf["20"]["inputs"]["unet_name"] = s2
            print("%-16s  %s → %s" % (chan,
                                      s1.replace(".safetensors", ""),
                                      s2.replace(".safetensors", "")))
            made.append(chan)
            if not write:
                continue
            d = os.path.join(SKILLS, chan)
            os.makedirs(d, exist_ok=True)
            _dump(os.path.join(d, "workflow.json"), wf)
            with open(os.path.join(d, "skill.md"), "w", encoding="utf-8") as f:
                f.write(SKILL_MD.format(
                    chan=chan, tier_cn=tier_cn, style_cn=style_cn,
                    tier_short=tier_cn, tier=tkey, s1=s1, s2=s2,
                    samp1=samp1, st1=st1, samp2=samp2, st2=st2,
                    scale=scale, out=out, style=style_cn,
                    style_desc=style_desc, style_words=style_words,
                    src=src))
    print("\n共 %d 个渠道%s" % (len(made), "" if write else "（干跑，加 --write 才写）"))


if __name__ == "__main__":
    main("--write" in sys.argv)
