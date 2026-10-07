"""把「三档 × 4 个画风」展开成 4 个生图渠道。

## 为什么是「组合」而不是 4 份手工工作流

画风（clear / soft / gloss / curvy）之间只差**两段各用哪块底模**；尺寸骨架
是同一份（三档：1024×1536 画布 → 1.5× latent → 1536×2304 → 末尾 2x 像素放大
→ 3072×4608）。两者正交，所以 4 份工作流 = 1 份骨架 × 4 组底模名字，没必要手抄。

**改的只有三处**：`UNETLoader(5)`（一段）和 `UNETLoader(20)`（二段）的
`unet_name`，加上本脚本统一注入的一对放大节点（`40` / `41`）。

> 历史：本脚本原先把 3 个尺寸档（hd_fast / hd_2 / hd_3）× 4 画风展开成 12 个
> 渠道。2026-10-07 用户拍板**取消快档 / 最小档 / 二档，只留三档**，并给三档末尾
> 加 2x 像素放大（原话：「只留一个三档，到时候就是你说 anima，就跑三档加上 x2
> 像素」）。裸骨架目录（`skills/hd_3`）当时已不在磁盘上，所以现在拿**已有的
> `hd_3_clear`** 当骨架——它本身就是三档骨架 + clear 底模，而 5 / 20 两处每轮
> 都会重写，拿它当骨架与拿裸骨架等价。

## 用法

    python _make_hd_channels.py          # 先干跑，看要建什么
    python _make_hd_channels.py --write  # 真写

写之前会打印每个渠道的「一段 → 二段」，对着看一眼再 --write。
"""

import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "skills")

REALSKIN = "miaomiaoRealskin_anima13.safetensors"
REALITY = "miaomiaoAnimeReality_ani11_3087842.safetensors"
HAREM = "miaomiaoHarem_anima16.safetensors"

# ── 末尾的 2x 像素放大（2026-10-07 加）──────────────────
# 与 silver / silver-hd 同一族的放大模型，只是倍率取 2x。纯像素放大：
# 不重采样、不加步数，接在 VAEDecode 之后、SaveImage 之前。
UPSCALE_MODEL = "2x_Ani4Kv2_G6i2_Compact_107500.pth"
UPSCALE_LOAD_NODE = "40"
UPSCALE_APPLY_NODE = "41"

# ── 画风 = 底模组合（一段 → 二段）────────────────────────
# 组合照抄取消前 4 个常规渠道（anima_clear / soft / gloss / curvy）的实测结果。
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

# ── 尺寸：只剩三档（2026-10-07 用户拍板）──────────────────
# (tier_key, 中文名, 骨架渠道, latent 放大, 最终输出, 一段采样, 一段步, 二段采样, 二段步)
TIERS = [
    ("3", "高清三档", "hd_3_clear", "1.5×", "3072×4608",
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


def _add_2x_upscale(wf):
    """在 VAEDecode 之后、SaveImage 之前插一对像素放大节点。

    节点号写死 40 / 41（跟 silver / silver-hd 同一套编号，方便对照）。

    **必须幂等**：本脚本的骨架 `src` 就是某个 `hd_3_*` 渠道，而它自己已经是
    注入过的产物——重跑时 40 / 41 一定已经在里面。这时只把连线 / 模型名对齐，
    不重插（重插会撞号）。只有**号被别的节点占了**才报错，那是骨架换了。
    """
    saves = [k for k, v in wf.items() if v.get("class_type") == "SaveImage"]
    if len(saves) != 1:
        raise SystemExit("期望恰好 1 个 SaveImage，实际 %d 个" % len(saves))
    save = saves[0]

    load = wf.get(UPSCALE_LOAD_NODE)
    apply_node = wf.get(UPSCALE_APPLY_NODE)
    if load is not None or apply_node is not None:
        if not (load and load.get("class_type") == "UpscaleModelLoader"
                and apply_node
                and apply_node.get("class_type") == "ImageUpscaleWithModel"):
            raise SystemExit(
                "节点 %s/%s 被别的节点占了（%r / %r），骨架变了，先改号"
                % (UPSCALE_LOAD_NODE, UPSCALE_APPLY_NODE,
                   (load or {}).get("class_type"),
                   (apply_node or {}).get("class_type")))
        load["inputs"]["model_name"] = UPSCALE_MODEL
        apply_node["inputs"]["upscale_model"] = [UPSCALE_LOAD_NODE, 0]
        wf[save]["inputs"]["images"] = [UPSCALE_APPLY_NODE, 0]
        return wf

    src = wf[save]["inputs"]["images"]          # 形如 ["3", 0] = VAEDecode
    if not isinstance(src, list) or len(src) != 2:
        raise SystemExit("SaveImage.images 不是连线：%r" % (src,))
    wf[UPSCALE_LOAD_NODE] = {
        "class_type": "UpscaleModelLoader",
        "inputs": {"model_name": UPSCALE_MODEL},
    }
    wf[UPSCALE_APPLY_NODE] = {
        "class_type": "ImageUpscaleWithModel",
        "inputs": {"upscale_model": [UPSCALE_LOAD_NODE, 0], "image": src},
    }
    wf[save]["inputs"]["images"] = [UPSCALE_APPLY_NODE, 0]
    return wf


SKILL_MD = """---
kind: 生图
---

# {chan} · {tier_cn} × {style_cn}

**{tier_cn}** 的尺寸，**{style_cn}** 的画风。

> 2026-10-07 用户拍板：anima 族的**快档 / 最小档 / 二档全取消，只剩三档**
> （`hd_3_<画风>` 这 4 个）。用户说「**anima**」或「三档 / 高清」→ 走本渠道；
> 没提画风就是 `hd_3_clear`。末尾带 2x 像素放大，输出 **3072×4608**。

| | 本渠道 |
|---|---|
| 一段底模 | `{s1}` |
| 二段底模 | `{s2}` |
| 一段采样 | `{samp1}` · {st1} 步 · cfg 5 |
| 二段采样 | `{samp2}` · {st2} 步 · cfg 5 · denoise 0.25 |
| 放大（latent）| `{scale}`（`nearest-exact`，夹在两段之间） |
| 放大（像素）| `{up_model}`（`ImageUpscaleWithModel`，接在末尾） |
| 画布 → 输出 | 1024×1536 → 1536×2304 → **{out}** |

## 画风：{style_cn}

{style_desc}。点词：{style_words}。

**画风 = 两段底模的组合**，跟取消前那 4 个常规渠道（`anima_clear` 等）是同一套
组合，只是画布更大、末尾多一道像素放大。四个画风：

| 画风 | 一段 → 二段 | 点词 |
|---|---|---|
| `clear` | Realskin → Realskin | 清透 / 素净 / 光别那么硬 |
| `soft` | Realskin → AnimeReality | 柔 / 素 / 干净 / 层次多一点 |
| `gloss` | AnimeReality → Realskin | 亮面 / 油光 / 冷色 |
| `curvy` | Harem → AnimeReality | 丰满 / 大胸 / 光影强 |

> 用户只说「anima / {tier_short}」没说画风 → 走 `hd_3_clear`（默认画风）。

## 尺寸：{tier_cn}

**这是 anima 族现在唯一的一档**——快档（`hd_fast_*`）、最小档（`anima_*`）、
二档（`hd_2_*`）已于 2026-10-07 全部取消。用户还在说「二档 / 快档」时，
**当没说过、照跑三档**（代码里旧档位词一律映射到 `hd_3_*`）。

> ⚠️ 它是最重的一档：画布 1024×1536 起步，末尾还要 2x 像素放大到 3072×4608。
> 本机 6GB 显存，显存吃紧时会比较慢。

## 提示词

节点 4 的模板是 `@kibro, __MULTI_PROMPTS__`。`@kibro` 是 kibro LoRA 的触发词，
**不能丢**。负向在节点 8，未改动。

## 怎么来的（别手改 workflow.json）

本渠道由 `_make_hd_channels.py` 从**尺寸骨架** + **画风底模组合**生成，
末尾那对放大节点（`40` / `41`）也是脚本统一注入的：

```bat
cd /d D:\\AI\\agent_my_test
python _make_hd_channels.py --write
```

骨架取自现有渠道 `{src}`（用
`_sync_comfy_workflow.py "<ComfyUI 里的工作流名>" {src} --prefix "@kibro, "` 重导）。
**改尺寸参数改骨架、改画风改底模、改放大模型改脚本里的 `UPSCALE_MODEL`，
然后重跑脚本**——手改某一份会让 4 个渠道之间悄悄不一致。

改完跑 **`python -m unittest tests.test_image_channels`** 验收——渠道集合、
两段拓扑、末尾放大、输出尺寸这些架构断言都在那个文件里钉着，
`tests.test_image_channels` 也是「谁动了渠道必须同步改测试」的入口。
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
            _add_2x_upscale(wf)
            print("%-16s  %s → %s  +2x(%s)" % (
                chan,
                s1.replace(".safetensors", ""),
                s2.replace(".safetensors", ""),
                UPSCALE_MODEL))
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
                    scale=scale, out=out, style=style_cn, up_model=UPSCALE_MODEL,
                    style_desc=style_desc, style_words=style_words,
                    src=src))
    print("\n共 %d 个渠道%s" % (len(made), "" if write else "（干跑，加 --write 才写）"))


if __name__ == "__main__":
    main("--write" in sys.argv)
