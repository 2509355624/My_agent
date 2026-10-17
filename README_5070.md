# 渠道包（skills/）—— 给 5070 那台机器

这个 zip 里**只有一样东西：`skills/` 目录**。代码你那边已经 `git pull` 过了，所以这里
不含代码、不含 `.env`、不含 `agents/`。

---

## 里面有什么

**16 个生图渠道**（4 档 × 4 画风），每个目录含 `skill.md` + `workflow.json`：

| 档位 | 目录 | 画布 |
|---|---|---|
| 常规档 | `anima_clear` `anima_soft` `anima_gloss` `anima_curvy` | 1024×1536 |
| 高清快档 | `hd_fast_*` | 1024×1536 |
| 高清二档 | `hd_2_*` | 1328×2000 |
| 高清三档 | `hd_3_*` | 1328×2000 |

**12 个图生图工作流 `workflow_i2i.json`** —— 本次新增，**就是你说缺的那个**：

- 只给 `anima_*` / `hd_fast_*` / `hd_2_*`（共 12 个）；`hd_3_*` 故意不给（三档本身
  纯执行就约 150 秒，叠上垫图两分半起）。
- 内容是「同目录文生图工作流删掉 `EmptyLatentImage`、换成 `LoadImage + VAEEncode`，
  一段 `denoise` 换成 `__DENOISE__` 占位符」，其余（采样器 / LoRA / 二段放大 /
  提示词模板）一律不动。

其它技能：`image_presets`、`krea2`、`nffa`（Illustrious 系画风，画布 1024×1536、出图 1128×1688，手+脸两段修复，
一张 40~75 秒）、`night-visit`、`nsfw_*`、`pose_library`、
`goutoujunshi`、`writing` 等，一并在内。

---

## 怎么装（3 步）

1. **先备份**你那边现有的 skills（保险起见）：
   ```
   cd D:\AI\<你的项目目录>
   ren skills skills_bak
   ```
   （或者把 `skills` 整个复制一份改名也行。）

2. 把这个 zip **解压到项目根目录**，让解压出来的 `skills\` 正好落在项目根下
   （和 `app\`、`agents\` 同级，不要多套一层文件夹）。

3. 重启机器人适配层：关掉 `QQBOT-ADAPTER` 窗口，双击 `启动QQ机器人.bat`。
   **不用重启 NapCat，也不用重启 ComfyUI。**

---

## 装完要检查的

**① 模型（最关键）**

工作流里引用模型用的是**纯文件名**（不是绝对路径），ComfyUI 会在自己的 models
目录里按文件名找。除了底模，还必须有下面这几样，少一个提交时就报「找不到模型」：

| 类型 | 文件名 |
|---|---|
| VAE | `qwen_image_vae.safetensors` |
| CLIP | `qwen_3_06b_base.safetensors` |
| LoRA | `画风-anima-baka skin.safetensors` |
| LoRA | `画风-anima-okitatsuki-kibro.safetensors` |
| 底模 | `miaomiaoRealskin_anima13.safetensors` 等（各渠道可能不同） |
| 底模 | `waiIllustriousSDXL_v150.safetensors`（仅 `nffa`） |
| LoRA | `NffaV1.3-000018.safetensors`（仅 `nffa`，就是它的画风本体） |
| 检测模型 | `ultralytics/bbox/hand_yolov8s.pt`、`ultralytics/bbox/face_yolov8m.pt`（仅 `nffa`） |

⚠️ `nffa` 还依赖自定义节点包 **comfyui-impact-pack**（工作流里的 `FaceDetailer` /
`UltralyticsDetectorProvider`）——那台机器没装的话，提交时报的是「未知节点类型」，
不是「找不到模型」。不装就别传 `skill=nffa`。

**② `.env`（可选）**

水位参数不写也有默认值，不影响跑：

- `COMFY_MIN_FREE_VRAM_GB`（默认 2.0）—— 这值是按 6GB 笔记本定的保守值，
  5070 显存大，可以调高，比如 4.0。
- `COMFY_MIN_FREE_RAM_GB`（默认 2.0）。

**③ `run_comfyui.bat`（在 `D:\AI\` 下，不在 git 里，得手动看一眼）**

我这边把 `--cache-none` 去掉了（它让每次跑图每个节点都重算）。你那边如果还带着
这个参数，建议也去掉。

---

## 怎么验证

- 在群里**引用一张机器人发的图**，说「把这张改成夜景」→ 应走**图生图**
  （出图尺寸跟该渠道画布一致）。
- 只引用图、说「照这个画一张新的」→ 应走**文生图**（不看原图，只反推提示词）。
- `hd_3_*` 渠道收到改图请求应被拒绝（这个档位不支持图生图）。
