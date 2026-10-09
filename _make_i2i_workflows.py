"""给支持的渠道各生成一份 workflow_i2i.json（图生图骨架）。

## 两种骨架，别搞混

### A. 重绘档（`to_i2i`）：anima_* / hd_fast_* / hd_2_*，共 12 个

**只差 latent 从哪来。** 文生图是 `EmptyLatentImage(9)` 凭空起一张；图生图是
`LoadImage → VAEEncode` 把垫图编码成 latent。采样器、步数、CFG、放大倍率、
LoRA、提示词模板、二段精修——**一个都不动**。

所以这一路做的事就三件：

1. 删掉 `EmptyLatentImage`；
2. 加 `LoadImage`（`image = "__SOURCE_IMAGE__"`）+ `VAEEncode`（vae 接原来的
   `VAELoader`），把一段 KSampler 的 `latent_image` 指过去；
3. 一段 KSampler 的 `denoise` 从 1 换成 `"__DENOISE__"` 占位符（文生图是纯噪声
   起步，图生图要留一部分原图）。

**二段的 `LatentUpscaleBy.scale_by` 照抄不动。** 于是出图尺寸 = 源图按比例缩到
本档画布长边之后，再乘这个倍率。源图多大不影响出图多大——「高清档」才是高清档。
运行时负责把源图缩到画布长边（画布从同目录的 workflow.json 里读，不另立元数据）。

⚠️ 这 12 个渠道 2026-10-07 已被 `app/skills.ARCHIVED_SKILLS` **屏蔽**（对外不可见、
模型点不到），但**骨架照旧留着**——屏蔽只挡可见性那一层。

### B. 锁构图档（`to_lllite`）：silver / silver-hd / hd_3_*，共 6 个

**latent 一点都不动。** `EmptyLatentImage` 留着、一段 `denoise` 仍是 1.0——
出图是**重新画**的，不是把参考图编码进 latent 再采样。参考图只走一条 LLLite
支路，经 `Canny → ImageInvert` 提成线稿后，由 `AnimaLLLiteApply` 往模型的每一层
DiT block 注入一个 32 维 FiLM 偏置（低秩条件，**不是 ControlNet**）。

于是：**出图尺寸永远是本档自己的画布**（silver 1024×1536 → 1.2× → 1228×1843，
hd_3 1024×1536 → 1.5× → 2× → 3072×4608），参考图只当构图条件、不参与定尺寸。

实测（2026-10-09，本机 5070）：
- **耗时 +0 秒**（无 LLLite 39.1~42.0s vs 带 LLLite 42.1s）；
- 同 seed 两次跑像素差 **0.0000**（完全确定性）；
- 提示词与参考图**冲突时提示词赢**——32 维 FiLM 装不下空间布局，别指望姿态复刻；
- 提示词与参考图**一致时**边缘相关性 **0.13 → 0.39 / 0.51**（噪声底 0.08~0.12）。

⚠️ 依赖 `models/model_patches/anima-lllite-any-test-like-v2.safetensors`。本机
`ComfyUI_v037/extra_model_paths.yaml` **没有 `model_patches` 项**，那份补丁是手工
拷进 `models/model_patches/` 的——换机器 / 重装后不加这一行，这条链会静默失败。

**qwen_image_v1 不归这个脚本管**：它的图生图是**编辑**不是重绘——参考图走
`TextEncodeQwenImage21` 的 conditioning，不是 `LoadImage → VAEEncode`，denoise
也必须写死 1（骨架吃的是节点吐的空 latent）。那份 `workflow_i2i.json` 是手写的，
`channels()` 里刻意没有它，别把它加进来让脚本按重绘的模板覆写。

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

# A 路：垫图重绘（删 EmptyLatentImage、加 VAEEncode、denoise 换占位符）
REDRAW_TIERS = ("anima", "hd_fast", "hd_2")
# B 路：锁构图（latent 不动，模型链前串一条 LLLite 支路）
LLLITE_CHANNELS = ("silver", "silver-hd") + tuple("hd_3_" + s for s in STYLES)

T2I = "workflow.json"
I2I = "workflow_i2i.json"

# ── B 路的参数 ───────────────────────────────────────────────────────
# 改这里 = 必须重跑本脚本，并且**同步改 `tests/test_image_i2i.py` 里那份断言**
# （那边钉的就是这几个数，它读的是生成出来的 JSON，不是这几个常量）。
# 取值出处：作者产线那 168 份工作流用的就是 any-test-like-v2 + 0.92/0.92；
# 实测 0.92 与 1.00 几乎没有差别（边缘相关性 0.3943 vs 0.3942）。
LLLITE_PATCH = "anima-lllite-any-test-like-v2.safetensors"
LLLITE_STRENGTH = 0.92
LLLITE_END = 0.92
CANNY_LOW = 0.12
CANNY_HIGH = 0.3


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


def _stage1_of(wf):
    """按连线找「吃空 latent 的那个 KSampler」= 一段。

    节点 id 每个渠道都不一样，只能按连线找：二段吃的是 `LatentUpscaleBy`。
    """
    lat_ids = _find(wf, "EmptyLatentImage")
    if len(lat_ids) != 1:
        raise SystemExit("预期恰好 1 个 EmptyLatentImage，实际 %d 个" % len(lat_ids))
    lat_id = lat_ids[0]
    stage1 = [nid for nid, n in wf.items()
              if n.get("class_type") == "KSampler"
              and n["inputs"].get("latent_image") == [lat_id, 0]]
    if len(stage1) != 1:
        raise SystemExit("找不到唯一的一段 KSampler（%s）" % stage1)
    return stage1[0], lat_id


def to_lllite(wf):
    """文生图骨架 → 锁构图骨架。就地改传入的 dict，返回改了什么（供打印）。

    和 `to_i2i` 的区别是**不碰 latent**：`EmptyLatentImage` 留着、一段 `denoise`
    仍是 1.0。参考图只走 LLLite 那条支路，出图是重新画的。

    插入点：一段 KSampler 的 `model` 源（模型链的尾巴，通常是个
    `LoraLoaderModelOnly`）后面接 `AnimaLLLiteApply`，再让一段改吃它的输出。
    **二段不动**——它在 denoise 0.25 上只做细节精修，构图早定了。
    """
    stage1, lat_id = _stage1_of(wf)

    model_src = wf[stage1]["inputs"].get("model")
    if not model_src:
        raise SystemExit("一段 KSampler 没有 model 输入")
    src_id = model_src[0]
    if src_id not in wf:
        raise SystemExit("一段 KSampler 的 model 指向不存在的节点 %s" % src_id)

    # 模型链的尾巴必须只喂一段。要是还有别的消费者，把 LLLite 串进去会顺带
    # 改到它（例如二段共用一个底模）——那种情况得另想办法，别硬插。
    others = [nid for nid, n in wf.items()
              if nid != stage1
              and (n.get("inputs") or {}).get("model") == model_src]
    if others:
        raise SystemExit("模型链尾 %s 还被 %s 消费，不能独占插入" % (src_id, others))

    # 新节点 id 取现有最大值往上排，不跟任何已有 id 撞。
    nid = max(int(k) for k in wf) + 1
    patch_id, load_id, canny_id, inv_id, apply_id = (
        str(nid + i) for i in range(5))

    wf[patch_id] = {"class_type": "ModelPatchLoader",
                    "inputs": {"name": LLLITE_PATCH}}
    wf[load_id] = {"class_type": "LoadImage",
                   "inputs": {"image": "__SOURCE_IMAGE__", "upload": "image"}}
    wf[canny_id] = {"class_type": "Canny",
                    "inputs": {"image": [load_id, 0],
                               "low_threshold": CANNY_LOW,
                               "high_threshold": CANNY_HIGH}}
    wf[inv_id] = {"class_type": "ImageInvert",
                  "inputs": {"image": [canny_id, 0]}}
    wf[apply_id] = {"class_type": "AnimaLLLiteApply",
                    "inputs": {"model": model_src,
                               "model_patch": [patch_id, 0],
                               "image": [inv_id, 0],
                               "strength": LLLITE_STRENGTH,
                               "start_percent": 0.0,
                               "end_percent": LLLITE_END}}
    wf[stage1]["inputs"]["model"] = [apply_id, 0]

    return {
        "canvas": "%dx%d" % (wf[lat_id]["inputs"]["width"],
                             wf[lat_id]["inputs"]["height"]),
        "scale_by": [n["inputs"]["scale_by"] for n in wf.values()
                     if n.get("class_type") == "LatentUpscaleBy"][0],
        "stage1": stage1,
        "model_src": src_id,
        "model_src_kind": wf[src_id]["class_type"],
        "lllite": apply_id,
    }


def channels():
    """产出 `(渠道名, 骨架类型)`。类型只有 `"redraw"` / `"lllite"` 两种。"""
    for tier in REDRAW_TIERS:
        for style in STYLES:
            yield tier + "_" + style, "redraw"
    for name in LLLITE_CHANNELS:
        yield name, "lllite"


def main():
    write = "--write" in sys.argv
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1]
        if only not in ("redraw", "lllite"):
            raise SystemExit("--only 只认 redraw / lllite")

    todo = [(c, m) for c, m in channels() if only in (None, m)]
    redraw = [c for c, m in todo if m == "redraw"]
    lllite = [c for c, m in todo if m == "lllite"]

    print("要生成：%d 个" % len(todo))
    print("  A 垫图重绘 %2d 个：%s" % (len(redraw), " / ".join(redraw) or "（无）"))
    print("  B 锁构图   %2d 个：%s" % (len(lllite), " / ".join(lllite) or "（无）"))
    print()

    made, same, skipped, changed = 0, 0, [], []
    for chan, mode in todo:
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

        if mode == "redraw":
            info = to_i2i(wf)
            detail = "画布 %s 长边 %d 二段 %s× 一段 denoise %s -> __DENOISE__" % (
                info["canvas"], info["canvas_long"], info["scale_by"],
                info["old_denoise"])
        else:
            info = to_lllite(wf)
            detail = "画布 %s 二段 %s× 一段 model %s(%s) -> LLLite@%s" % (
                info["canvas"], info["scale_by"], info["model_src"],
                info["model_src_kind"], LLLITE_STRENGTH)

        out = os.path.join(d, I2I)
        text = dump_json(wf)
        note = "（新建）"
        if os.path.exists(out):
            with open(out, encoding="utf-8") as f:
                old_text = f.read()
            if old_text == text:
                note = "（与现有文件逐字节相同）"
                same += 1
            else:
                note = "（⚠️ 与现有文件不同）"
                changed.append(chan)

        print("%-13s %-7s %s %s" % (chan, mode, detail, note))

        if write:
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            made += 1

    if skipped:
        print("\n跳过（没有 %s）：%s" % (T2I, "、".join(skipped)))
    print()
    if write:
        print("已写入 %d 份 %s（其中 %d 份与原来逐字节相同）" % (made, I2I, same))
    else:
        print("干跑完毕。确认上面这些行没问题，再加 --write。")
    if changed:
        print("⚠️ 这 %d 份内容变了：%s" % (len(changed), "、".join(changed)))


if __name__ == "__main__":
    main()
