"""给支持的渠道各生成一份 workflow_i2i.json（图生图骨架）。

## 图生图和文生图差在哪

**只差 latent 从哪来。** 文生图是 `EmptyLatentImage(9)` 凭空起一张；图生图是
`LoadImage → VAEEncode` 把垫图编码成 latent。采样器、步数、CFG、放大倍率、
LoRA、提示词模板、二段精修——**一个都不动**。

所以本脚本做的事就三件：

1. 删掉 `EmptyLatentImage`；
2. 加 `LoadImage`（`image = "__SOURCE_IMAGE__"`）+ `VAEEncode`（vae 接原来的
   `VAELoader`），把一段 KSampler 的 `latent_image` 指过去；
3. 一段 KSampler 的 `denoise` 从 1 换成 `"__DENOISE__"` 占位符（文生图是纯噪声
   起步，图生图要留一部分原图）。

**二段的 `LatentUpscaleBy.scale_by` 照抄不动。** 于是出图尺寸 = 源图按比例缩到
本档画布长边之后，再乘这个倍率。源图多大不影响出图多大——「高清档」才是高清档。
运行时负责把源图缩到画布长边（画布从同目录的 workflow.json 里读，不另立元数据）。

## 支持哪些档

anima_*（常规）+ hd_fast_* + hd_2_*，共 12 个渠道。
**hd_3_* 不做**：三档本身就贵（1.5× + 二段 10 步，实测单张 ~150 秒），叠上图生图
要两分半以上，不值当。

## 用法

    python _make_i2i_workflows.py          # 先干跑
    python _make_i2i_workflows.py --write  # 真写

干跑会顺手做一次「读进来再原样写回去」的比对：跟原文件逐字节相同才说明这份
序列化风格（缩进、裸 `__SEED__`、中文不转义）跟现有 workflow.json 一致。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "skills")

STYLES = ("clear", "soft", "gloss", "curvy")
TIERS = ("anima", "hd_fast", "hd_2")   # 不含 hd_3

T2I = "workflow.json"
I2I = "workflow_i2i.json"


def load_json(path):
    """读工作流。裸 `__SEED__` 不是合法 JSON，先临时包上引号。"""
    with open(path, encoding="utf-8") as f:
        return json.loads(f.read().replace(": __SEED__", ': "__SEED__"'))


def dump_json(wf):
    """按现有 workflow.json 的风格序列化：缩进 2、中文不转义、`__SEED__` 回裸 token。"""
    return (json.dumps(wf, indent=2, ensure_ascii=False)
            .replace('"__SEED__"', "__SEED__") + "\n")


def _find(wf, class_type):
    return [nid for nid, n in wf.items() if n.get("class_type") == class_type]


def to_i2i(wf):
    """文生图骨架 → 图生图骨架。就地改传入的 dict，返回改了什么（供打印）。"""
    lat_ids = _find(wf, "EmptyLatentImage")
    if len(lat_ids) != 1:
        raise SystemExit("预期恰好 1 个 EmptyLatentImage，实际 %d 个" % len(lat_ids))
    lat_id = lat_ids[0]

    vae_ids = _find(wf, "VAELoader")
    if len(vae_ids) != 1:
        raise SystemExit("预期恰好 1 个 VAELoader，实际 %d 个" % len(vae_ids))
    vae_id = vae_ids[0]

    # 一段 = 吃 EmptyLatentImage 的那个 KSampler（二段吃的是 LatentUpscaleBy，
    # 认不出来。节点 id 每个渠道都不一样，只能按连线找）。
    stage1 = [nid for nid, n in wf.items()
              if n.get("class_type") == "KSampler"
              and n["inputs"].get("latent_image") == [lat_id, 0]]
    if len(stage1) != 1:
        raise SystemExit("找不到唯一的一段 KSampler（%s）" % stage1)
    stage1 = stage1[0]

    lat = wf[lat_id]["inputs"]
    old_denoise = wf[stage1]["inputs"].get("denoise")

    # 新节点 id 取现有最大值往上排，不跟任何已有 id 撞。
    nid = max(int(k) for k in wf) + 1
    load_id, enc_id = str(nid), str(nid + 1)

    wf.pop(lat_id)
    wf[load_id] = {"class_type": "LoadImage",
                   "inputs": {"image": "__SOURCE_IMAGE__", "upload": "image"}}
    wf[enc_id] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": [load_id, 0], "vae": [vae_id, 0]}}
    wf[stage1]["inputs"]["latent_image"] = [enc_id, 0]
    wf[stage1]["inputs"]["denoise"] = "__DENOISE__"

    return {
        "canvas": "%dx%d" % (lat["width"], lat["height"]),
        "canvas_long": max(lat["width"], lat["height"]),
        "scale_by": [n["inputs"]["scale_by"] for n in wf.values()
                     if n.get("class_type") == "LatentUpscaleBy"][0],
        "stage1": stage1,
        "lat_id": lat_id,
        "load_id": load_id,
        "enc_id": enc_id,
        "old_denoise": old_denoise,
    }


def channels():
    for tier in TIERS:
        for style in STYLES:
            yield tier + "_" + style


def main():
    write = "--write" in sys.argv

    print("支持图生图的渠道：%d 个（%s）\n" % (
        len(TIERS) * len(STYLES), " / ".join(TIERS)))
    print("%-16s %-11s %-9s %-8s %s" % ("渠道", "画布", "长边", "二段倍率", "一段 denoise"))
    print("-" * 62)

    made, skipped = 0, []
    for chan in channels():
        d = os.path.join(SKILLS, chan)
        src = os.path.join(d, T2I)
        if not os.path.exists(src):
            skipped.append(chan)
            continue

        wf = load_json(src)
        # 自检：读进来原样写回去，必须跟原文件逐字节相同，否则说明序列化风格
        # 跟现有文件不一致（缩进 / 裸 __SEED__ / 中文转义），得先修这里。
        with open(src, encoding="utf-8") as f:
            if dump_json(wf) != f.read():
                raise SystemExit("序列化风格对不上：" + src)

        info = to_i2i(wf)
        out = os.path.join(d, I2I)
        print("%-16s %-11s %-9d %-8s %s -> __DENOISE__" % (
            chan, info["canvas"], info["canvas_long"], info["scale_by"],
            info["old_denoise"]))

        if write:
            with open(out, "w", encoding="utf-8") as f:
                f.write(dump_json(wf))
            made += 1

    if skipped:
        print("\n跳过（没有 %s）：%s" % (T2I, "、".join(skipped)))
    print()
    if write:
        print("已写入 %d 份 %s" % (made, I2I))
    else:
        print("干跑完毕。确认上面这些行没问题，再加 --write。")


if __name__ == "__main__":
    main()
