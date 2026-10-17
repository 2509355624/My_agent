# anima多图渠道 · 设计（已落地）

状态：**已落地**（2026-10-16）。渠道名「**anima多图**」；渲染走**双模型两段采样**
（复刻 anima线条 结构）。改动落在 `app/comic_story.py` / `app/direct_gen.py` /
`app/tools/normal/generate_comic.py` / `app/image_jobs.py` / `app/tools/normal/comfy_workflow.py`，
新增 `skills/_anima_multi_batch/`，以及 **ComfyUI 自定义节点**
`DualModelBatchPromptImageGenerator`。

## 1. 目标与触发

用户想在 QQ 里直接说：

```
anima多图，<需求>
```

就**一次快速出多张**符合需求的图，**默认 5 张**（silver 漫画仍是默认 10 张）。
触发词以「anima多图」为准（容忍中间空格，`anima\s*多图`，大小写不敏感）。

> 本质：针对用户这个需求，快速产出多个符合需求的图片。所以走 anima 快配方 + 少量
> 张数（5），而不是 silver 的 10 格剧情。

## 2. 现状（改动前）

QQ 侧漫画**只有一条渲染路**：

- 入口：`direct_gen` 的 JSON 契约 `{"tool":"generate_comic","brief":..., "panels":...}`
  → `_enqueue_comic` → `generate_comic._generate_comic` → `comic_story.start/render`。
  （QQ 的 @ 轮不跑主 Agent 循环，注册在工具表里的 `generate_comic` 在 QQ 里
  永远调不到，漫画只能从这条 JSON 契约进来。）
- 渠道：`comic_story.COMIC_SKILL = "_comic_batch"`（**写死单值**），单段批量。
- 工作流 `skills/_comic_batch/`：silver 底模 + 6 LoRA +
  `BatchPromptImageGenerator`（816×1024、10 步、cfg1、er_sde/simple）+ 2x 像素放大。

## 3. 设计（落地形态）

### 3.1 新增内部批量工作流 `skills/_anima_multi_batch/` + 新节点

**双模型两段采样**，复刻 `anima线条` 的结构：一段「anima 底模 + 6 LoRA」，
中间 latent 放大，二段换**另一个底模**（silvermoonmix 裸接）再跑一遍。

为什么必须新建节点：原生 `BatchPromptImageGenerator` 只有一个 `model` 输入、
二段沿用一段的 `sampler_name`/`scheduler`——做不出「二段换底模 + 换采样器」。
2026-10-16 用户拍板：「现有的做不到就新建节点」。

新节点 `DualModelBatchPromptImageGenerator`
（`D:\AI\ComfyUI_v037\custom_nodes\ComfyUI-DualModelBatchGenerator\`）在原批量节点
骨架上加：`model_2` 输入、独立二段采样参数（`stage2_steps/cfg/sampler_name/scheduler/denoise`）、
两段间 latent 放大 `stage2_latent_scale`。批量语义（`---` 分隔、`base_prompt` 逐格拼、
`random_seed_per_prompt`、`save_inline`）原样保留。

| | 一段 | 二段 |
| :--- | :--- | :--- |
| 底模 | `miaomiaoRealskin_anima13` + 6 LoRA | `silvermoonmixAnimaEvolved_v2329BTurbo`（裸接） |
| 采样 | `er_sde` · simple · 4 步 · cfg 1 · denoise 1 | `euler` · simple · 4 步 · cfg 1 · denoise 0.25 |
| 中间 | — | latent ×1.2（nearest-exact，对应 `LatentUpscaleBy`） |

- 画布 `1024×1536`（对齐 anima线条）→ latent ×1.2 = 1232×1840 → 末尾 `2x_Ani4Kv2` 像素放大 → **2464×3680**。
- ⚠️ **两段之间 latent 放大的坑（2026-10-16 实测）**：anima（Qwen-Image 系）
  采样后的 latent 是 **5 维** `(B, C, F, H, W)`，宽高必须取**末尾两维**
  （`shape[-2]`/`shape[-1]`）。写成 `shape[2]` 会拿到帧维 F(=1)，被
  `max(8, ...)` 钳成 8 → 高度崩到 64px（实测出图 `1728×128`，5 张全被审核
  拦下）。隔离实验：scale=1.2 → 864×64；scale=1.0（走早退分支）→ 1440×1440。
- 两段共用同一个种子 / 同一份正负条件。
- `save_inline=true`，`filename_prefix="Anima_Multi"`，`delimiter="---"`，
  `multi_prompts` 落 `__MULTI_PROMPTS__`。

节点清单（对照 `_comic_batch/workflow.json`）：

```
6  CLIPLoader  qwen_3_06b_base / stable_diffusion
1  VAELoader   qwen_image_vae
5  UNETLoader  miaomiaoRealskin_anima13
16 LoraLoaderModelOnly(0.35 acd)      ← 5
15 LoraLoaderModelOnly(0.35 star cat) ← 16
28 LoraLoaderModelOnly(0.30 MODARE)   ← 15
29 LoraLoaderModelOnly(0.80 cowani)   ← 28
30 LoraLoaderModelOnly(0.80 acd)      ← 29
31 LoraLoaderModelOnly(0.80 reina)    ← 30
36 UNETLoader  silvermoonmixAnimaEvolved_v2329BTurbo   ← 二段底模，裸接
8  CLIPTextEncode  negative
35 UpscaleModelLoader 2x_Ani4Kv2_G6i2_Compact_107500.pth
33 DualModelBatchPromptImageGenerator
   model←31  model_2←36  clip←6  vae←1  negative←8  upscale_model←35  multi_prompts←32
   base_prompt=<画师串>  delimiter="---"
   width=720 height=720 seed=__SEED__
   steps=4 cfg=1 sampler_name=er_sde scheduler=simple denoise=1
   stage2_steps=4 stage2_cfg=1 stage2_sampler_name=euler stage2_scheduler=simple stage2_denoise=0.25
   stage2_latent_scale=1.2
   random_seed_per_prompt=true  save_inline=true  filename_prefix=Anima_Multi
32 String Literal  __MULTI_PROMPTS__
```

### 3.2 `app/comic_story.py`：单值 → 映射

- `COMIC_SKILLS = {"silver": "_comic_batch", "anima": "_anima_multi_batch"}`
  （保留 `COMIC_SKILL` 作为 silver 的别名）。
- `DEFAULT_PANELS_BY_STYLE = {"silver": 10, "anima": 5}` + `default_panels(style)`。
- `render/start` 增 `style` 参数，内部 `skill = COMIC_SKILLS.get(style or "silver", COMIC_SKILL)`。

### 3.3 `app/tools/normal/generate_comic.py`

- `_generate_comic(brief, panels=None, style="silver")`；`style` 透传。
- 工具 `parameters` 增可选 `style`（封闭 `enum: ["silver","anima"]`）。
- 回执按 style 区分：anima →「多图已开工：N 张，正在画，画完一起发。」

### 3.4 `app/direct_gen.py`：识别触发词

- 代码侧识别（权威）：`_ANIMA_MULTI_RE = re.compile(r"anima\s*多图", re.I)`，
  `_comic_style(text)` 判 → `"anima"`，否则 `""`。
- JSON 契约里 `generate_comic` 说明点明「anima多图」，但**没有 `style` 字段**——
  走哪条路由**代码**从原话判定。
- **2026-10-16 补丁（实测翻车后加）**：`decide` 里新增一条**确定性专路**，
  放在 `_parse_channel` 与「引用图轮 / 渠道词轮」**之前**——
  原话匹配 `_ANIMA_MULTI_RE` 就直接 `_enqueue_comic({brief, panels}, text)`，
  不走 `_translate`（brief 取用户原话，`write_story` 那层再扩写成 tag）。
  **为什么必须这样**：`anima` 在 `_TIER_RE` 里 ≡ 三档，不拦就会被 `_parse_channel`
  吃成 `hd_3_clear`（再把 `hd_3_clear` 当 `chan_hint` 明写给 AI → AI 永远不出
  comic 契约）；带引用图时还会被改图管道 `_revise` 先截走、只出单张。
  实测实录：群 1104825153 发「anima 多图」→ `直达入队：hd_3_clear`。
  同时 `_parse_channel` 加了一道闸：命中 `_ANIMA_MULTI_RE` 时返回 `(None, 原文)`，
  不锁任何渠道。

### 3.5 代理侧适配

- `image_jobs._SIZE_NODE_TYPES` 加 `DualModelBatchPromptImageGenerator`
  （横版对调要能找到画布节点）。
- `comfy_workflow.GEN_HINTS` 加 `DualModelBatchPromptImageGenerator`
  （`get_workflow` 认出「采样生成节点」）。

## 4. 与原始 anima线条 的对照

| 项 | anima线条（原始） | 本渠道（双模型批量） |
|---|---|---|
| 一段底模 + LoRA | miaomiaoRealskin_anima13 + 6 LoRA | 同 |
| 二段底模 | silvermoonmix turbo（裸接） | 同 |
| 一段采样 | er_sde / simple | 同 |
| 二段采样 | euler / simple / denoise 0.25 | 同 |
| 两段间 | LatentUpscaleBy ×1.2 nearest-exact | 同（节点内 `stage2_latent_scale`） |
| 末尾放大 | 2x 像素（Ani4Kv2） | 同 |
| 画布 | 1024×1536 | 1024×1536（快跑，对齐用户 anima线条 工作流） |
| 出图数 | 1 张 | 一次 N 张（默认 5） |

## 5. 验证

- `tests/test_comic_story.py`：27 用例 OK。`AnimaChannelTest.test_anima_workflow_is_anima_recipe`
  钉住双模型拓扑：一段 model←LoRA 链尾 31、二段 model_2←36 silvermoonmix 裸载、
  `stage2_sampler_name=euler`、`stage2_latent_scale=1.2`。
- `tests/test_direct_gen.py`：207 用例 OK。
- 手工构建校验：`build_t2i_workflow('_anima_multi_batch', ...)` 两段参数正确。

## 6. 风险 / 注意

- ⚠️ **ComfyUI 必须重启一次**，新节点才会注册（实测重启前 `/object_info` 无此节点）。
- 双底模会来回换入换出，显存管理更重；`save_inline` + 逐格释放规避爆显存。
- `anima_context_detailer_base10` 挂两次（0.35 / 0.8），别去重。
- 出图仍是**一个** ComfyUI 任务、占一个并发名额，代理侧要等整批跑完才拿到图。
