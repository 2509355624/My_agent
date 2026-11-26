"""生图配置域：ComfyUI、渠道、显存/内存水位、NAI。

只放默认值和环境变量读取；ComfyUI 的调用和队列逻辑在 app/comfy_*、
app/image_jobs.py，不在这里。
"""

import os

# ─── ComfyUI ────────────────────────────────────────
# 没装 ComfyUI 的机器设为 false：不再注册 generate_image 工具，
# 避免 LLM 白白尝试调用一个必然失败的工具
ENABLE_IMAGE_GEN = os.getenv("ENABLE_IMAGE_GEN", "true").lower() not in ("0", "false", "no")

COMFYUI_URL = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188")
# 看门狗重启 ComfyUI 时使用的固定解释器和项目目录。
# 默认值对应本机实际启动命令，也允许以后迁移环境时只改 .env。
COMFYUI_PYTHON = os.getenv(
    "COMFYUI_PYTHON", "D:\\AI\\confyui_env\\Scripts\\python.exe"
)
COMFYUI_ROOT = os.getenv("COMFYUI_ROOT", "D:\\AI\\ComfyUI_v037")
# 重启 ComfyUI 时用的启动参数（看门狗与强杀重启共用，见 app/comfy_launch.py）。
# 2026-10-28 主实例是 nightly 0.43.0，三个参数缺一不可：
#   --disable-comfy-compiler  不加会 aimdo memory compile error 直接起不来
#   --enable-triton-backend   nightly 的 int8_convrot 走这条路径
#   --enable-cors-header "*"  网页端（5174）要能直接拉 8188 的图
# 端口不写在这份字符串里也行——`comfy_launch` 会按 COMFYUI_URL 对齐。
COMFYUI_LAUNCH_ARGS = os.getenv(
    "COMFYUI_LAUNCH_ARGS",
    "--listen 127.0.0.1 --port 8188 --disable-comfy-compiler "
    "--enable-triton-backend --enable-cors-header *",
)

# 默认生图渠道（2026-10-19）：用户没点名渠道、模型也没填 skill 时落这里。
#
# 为什么要做成配置：`skills/` 里没装的渠道等于不存在，写死一个名字就会让
# 「没点名渠道」的每一张图都去找一个不存在的 workflow 报错。默认值是 silver
# （本机一直这么用），把项目打包给别人时，对方只要在 .env 里改成他实际有的
# 那个渠道（比如 anima-line）即可，代码不用动。
#
# ⚠️ 消费方有三处，都读这个常量：`direct_gen._FALLBACK_SKILL`、
# `generate_image.T2I_DEFAULT_SKILL`、`card_maker.COVER_SKILL`。改默认渠道的
# 时候记得同步检查模板里对「默认渠道」的描述（见 direct_gen 里那条老注释）。
IMAGE_DEFAULT_SKILL = os.getenv("IMAGE_DEFAULT_SKILL", "silver").strip() or "silver"

# ComfyUI 的 output 目录，**只服务于「下载兜底」**：图其实早就写好在磁盘上了，
# 被掐掉的只是 HTTP 那一跳（RAM 守卫重启 ComfyUI 时撞上下载，实测两次丢图都
# 是这么来的）。这时直接从本目录把同名文件复制过来，比丢掉整格强。
# 留空 = 关掉兜底，只靠 app/comic.py 里 `_download` 的重试。
# 文件名取自 ComfyUI 的 history，是 SaveImage 写下的真实名字，所以**同名即同图**；
# 兜底故意不做「找最新一张」的猜测——猜错会静默落一张别的图，比报错难查得多。
COMFYUI_OUTPUT_DIR = os.getenv("COMFYUI_OUTPUT_DIR", "")

# 工作台的「低分辨率档」（2026-11-26）：画布降一档采样，放大链按算术补回
# 成品尺寸（见 `app/features/image/lowres.py`）。只对**工作台**（`/workspace/`，
# 将来的访客模式也在这一侧）生效——QQ 机器人、本地跑图台 `/gen` 都不走它，
# 那两边要的是原图。默认开：工作台的图出站前本来就要压到 ≤1MB，先画小一点
# 再放大，出图快、原图小。
WORKSPACE_LOWRES = os.getenv("WORKSPACE_LOWRES", "true").lower() not in ("0", "false", "no")

# 单张图的出图上限（秒）。**注意这是「一张」的时限，不是「一次调用」的**：
# 生图在 image_jobs 里是逐张串行排队的，计时从每张**真正开跑**算起（排队时
# 间不算，否则排在第 5 位的人还没轮到就被判超时了）。
#
# 但「开跑」= 任务提交进 ComfyUI 那一刻，**模型加载也算在里面**：模型不在显存
# 里时要现加载（冷启动约 20~30s，换渠道重载更久，qwen 的 Q4_K 还要反量化）。
# 所以时限要留出这段余量，否则每次冷启动的第一张都必被掐。
#
# 到点还没出图就把这张中断掉、顺手清掉 ComfyUI 里的残留任务和显存，让后面排
# 队的人先跑——一张卡住的图不该堵着所有人。
IMAGE_GEN_TIMEOUT = int(os.getenv("IMAGE_GEN_TIMEOUT", "180"))

# ─── ComfyUI 内存水位（自动重启）─────────────────────
#
# ComfyUI 的常驻内存**只涨不落**：每张图约 +600MB，`/free` 也降不下来
# （它只把权重从显存搬到 CPU，不删）。16GB 物理内存被挤干之后，GGUF 要从
# 磁盘重读（实测 5.5 秒 → 68 秒）、采样卡在 0/N 一百秒，最后被
# IMAGE_GEN_TIMEOUT 掐掉——整机还跟着换页变卡。
#
# 唯一能把内存真正还回去的手段是重启进程（走 ComfyUI-Manager 的
# /manager/reboot，Legacy 模式下 os.execv 原地重启、保留原命令行）。
#
# 这里定的是**系统可用内存**的下限：低于它就重启。用绝对水位而不是百分比，
# 因为要防的是「整机换页」这件事，跟总内存多大无关。
#
# 0 = 关掉这个功能（回到从前：只涨，从不主动重启）。
COMFY_MIN_FREE_RAM_GB = float(os.getenv("COMFY_MIN_FREE_RAM_GB", "3.0"))

# 两次**尝试**重启之间的最小间隔（秒）。防抖用：内存要是被别的程序吃掉的，
# 重启 ComfyUI 也救不回来，这个间隔保证不会退化成「每张图都重启」。
# 成功失败都算，所以重启接口被拒时也不会每张图刷一条警告。
COMFY_RESTART_MIN_GAP = float(os.getenv("COMFY_RESTART_MIN_GAP", "300"))

# 等 ComfyUI 重启回来的上限（秒）。冷启动到能接第一张图约 60 秒，留够余量。
COMFY_RESTART_WAIT = float(os.getenv("COMFY_RESTART_WAIT", "180"))

# 提交前等 ComfyUI 在线的上限（秒）。与上面那个的区别：COMFY_RESTART_WAIT 是
# 「我们主动重启它、再等它回来」，这个是「它被别的进程/用户重启了，我们还没
# 提交，先等它活」。实测重启 14~22 秒，留 120 秒够慢启动。
COMFY_WAIT_UP = float(os.getenv("COMFY_WAIT_UP", "180"))

# ─── ComfyUI 显存水位（提交前先 /free）───────────────
#
# ComfyUI **从不把上一个任务清干净**。日志里那句
#   Unloaded partially: 2896.25 MB freed, 1591.04 MB remains loaded
# 就是证据：它每次只卸一部分，残留 1.6~2.1GB 会一路叠上去。实测 qwen 连画
# 16:09 成 / 16:12 成 / 16:14 崩，看着像残留累积。
#
# 现有逻辑只在**换渠道**时打 /free，同渠道连画不释放——qwen 连画正好是唯一
# 漏掉的那种情况。低于这个水位就补一次 /free，把残留腾出来。
#
# 2.0 是实测定的：anima 跑完还剩约 4.9GB、qwen 跑完约 3.1GB —— 两种都不该触发
# （模型保持热的，下一张马上还要用）。真掉到 2GB 以下才是「残留把空间吃掉了」。
# 0 = 关掉（回到从前：只在换渠道时释放）。
#
# ⚠️ **2026-10-04：.env 里那份 5.0 已改回 2.0（.env 优先于这里的默认值）。**
# 5.0 是 **12GB 卡**上量出来的（anima 跑完剩 5.5GB，刚够不触发）。本机换过机器，
# 实测 **RTX 5070 / 11.94GB**：anima 仍剩约 4.9GB、qwen 剩约 3.1GB —— 都**恒低于
# 5.0**，于是每张提交前都白打一次 /free，把刚跑热的权重整个卸掉、下一张再去磁盘
# 重读 6GB 文本编码器（01:33:40 那次只腾出 0.1GB）。同渠道耗时因此差一倍：
# 热跑 49.2s vs 冷跑 109.4s。2.0 只在真见底时才动手。
#
# ⚠️ **这条不是 qwen 崩溃的解药**（2026-09-27 16:22 真机实测推翻）：
# ComfyUI 刚重启、显存全空 10.78GB、连第一张 qwen 照样崩（3 条 nvlddmkm 153）。
# 真正的天花板是**权重本身**：TE 6018MB + unet 4487MB = 10.5GB，而 12GB 卡
# 空闲时只有 10.78GB 可用（约 1.16GB 被桌面/浏览器占着），只剩约 0.5GB 给
# 激活值——1024×1024 采样时不够，所以是概率性崩，不是必崩。
# 日志 `loaded completely; 5129.88 MB usable` 也印证：算这个「可用」时
# 6018MB 的编码器还驻留着。
# 这条水位只管「别让残留把本就紧张的空间再吃掉一块」，是保险不是解药。
COMFY_MIN_FREE_VRAM_GB = float(os.getenv("COMFY_MIN_FREE_VRAM_GB", "2.0"))

# ─── 同渠道连画释放（2026-10-05 加）─────────────────────
#
# 上面那条水位是「快见底才动手」的兜底，防不住这种场景：同渠道连画时模型
# 保持热的（换渠道才 /free），但 ComfyUI 从不把上一张清干净——每张跑完
# `Unloaded partially: ... remains loaded`，**残留 1.6~2.1GB 一路叠上去**。
# qwen 权重 10.5GB / 显存 11.94GB，连画到第 3 张 UNet 就装不下了，每步从
# 内存搬 4.5GB，速度 ×8 → 撞 IMAGE_GEN_TIMEOUT（用户实录：第 3 张超级慢
# → 卡死超时）。而且一旦进了这个换页态，/free 也救不回来，只能重启。
#
# 这里的数 = 同一个渠道**连续提交满 N 张后，下一张提交前彻底重启 ComfyUI**
# （2026-10-05 用户拍板：直接重启而不是 /free——残留叠进换页态后 /free 救
# 不回来，重启是唯一能把显存+内存都真正还回去的手段；约 60~90 秒不能出图，
# 用户知情选定）。计数在换渠道/ComfyUI 重启后归零。
# 0 = 关掉（回到从前：同渠道连画永不释放，只靠上面那条水位兜底）。
COMFY_RELEASE_AFTER_SAME = int(os.getenv("COMFY_RELEASE_AFTER_SAME", "2"))

# 按渠道覆盖上面的默认阈值（2026-10-13 用户：anima 残留叠得慢，5 张才重启；
# qwen 权重 10.5GB 保持 2）。格式：COMFY_RELEASE_AFTER_SAME_MAP="anima-line=5"，
# 多条用逗号分隔。没列到的渠道用 COMFY_RELEASE_AFTER_SAME。
#
# 这里带了内置默认（`.env` 配了同名键就覆盖它）：`_persona_fast` 是**扮演态**
# 的配图渠道（480×720 / 3 步，见 skills/_persona_fast），每轮聊天都出一张。
# 按全局的 2 张就会每两轮撞一次「提交前彻底重启」（60~90 秒不能出图）——那等于
# 不能聊。它跟 anima 系是同一套权重、残留叠得一样慢，就按 anima 的口径给 8。
COMFY_RELEASE_AFTER_SAME_MAP = {"_persona_fast": 8}
for _kv in (os.getenv("COMFY_RELEASE_AFTER_SAME_MAP") or "").split(","):
    if "=" in _kv:
        _k, _v = _kv.split("=", 1)
        try:
            COMFY_RELEASE_AFTER_SAME_MAP[_k.strip()] = int(_v)
        except ValueError:
            pass

# 全局累计重启（2026-10-15 用户拍板）：上面的计数**换渠道就归零**——用户实录
# qwen → krea2 → qwen 轮着点，每条渠道都攒不满阈值，重启一直不触发，残留
# 照样叠到底（10-15 12:37 卡死现场：换渠道提交时内存只剩 0.9GB）。这条不看
# 渠道：自上次重启以来**总共**提交满 N 张，下一张提交前彻底重启。
# 注意它会盖住 map 里的例外（全局 2 < anima-line 的 5 时后者永远轮不到）；
# 0 = 关。
COMFY_RELEASE_AFTER_TOTAL = int(os.getenv("COMFY_RELEASE_AFTER_TOTAL", "0"))

# 漫画批量（`Job.batch == "comic"`）专用的全局累计阈值（2026-10-18 用户拍板）。
#
# 漫画一次跑一整本（默认 10 格），按全局的 2 张一重启，10 格就要停 3 次、
# 每次 60~90 秒等冷启动，整本白扔 3~5 分钟。用户实测单格 812×1024 / 10 步
# 只要 20 秒，所以他要求「给漫画工作台设置 6 个图片」——6 格重启一次：
# 既不会让 anima 的残留（1.6~2.1GB/张，叠得慢）叠爆 12G 卡，整本也只停一次。
# 0 = 跟随 COMFY_RELEASE_AFTER_TOTAL（不单独放宽）。
COMFY_RELEASE_AFTER_COMIC = int(os.getenv("COMFY_RELEASE_AFTER_COMIC", "6"))

# ─── 重渠道（qwen）优先度：2026-10-04 按用户要求关闭 ───────────────
#
# ⚠️ **2026-10-04 用户拍板关闭（原值 90）**。触发场景：他连着跑漫画加字
#   （连环 qwen i2i），每两张之间被强制插 90 秒，观感是「一个生图任务卡死
#   3 分钟」。关掉后 qwen 之间不再强制间隔，按入队顺序连着跑。
#
# **恢复办法**（真要回滚，改这三处）：① 这里或 .env 写 QWEN_COOLDOWN=90；
#   ② `skills._SKILL_PRIORITY` 加回 `{"qwen_image_v1": 5}`；
#   ③ `skills/qwen_image_v1/SKILL.md` 的 frontmatter 加回 `priority: 5`。
#   ②③ 少了任何一条，权重就回到 1，冷却窗不开（见 image_jobs._finish 的
#   `job.weight > 1` 判据）。
#
# 为什么当初要有它（保留备查）：qwen 一套权重 10.5GB（文本编码器 6018MB +
#   unet 4487MB），本机 12GB 显卡空闲可用只有 10.78GB。2026-09-27 实测的错误
#   模式是确定性的：**第 1 张必成、第 2 张必死**（提交后 2~6 秒日志断在
#   `got prompt` 中间 → nvlddmkm 153，有时整机重启）。外挂启动参数与更低的
#   量化都试到底、全被推翻（见 image_jobs 模块开头），所以当初只能从 agent
#   侧管：别让 qwen 紧接着 qwen 跑。冷却窗本意是显存/内存把 6GB 权重还回去
#   所花的时间——文档自己记着实测 **3 分钟**的间隔才足以自然恢复，90 秒已经
#   是打折后的折中值。**关掉 = 接受连跑风险**，出问题时表现是 ComfyUI 变僵尸
#   （8188 永久 500）或整机重启。
#
# ⚠️ 这个数值**跟「一张图跑多快」无关**：步数（10 / 12 / 20）只决定采样迭代
#   几轮，权重加载量一个字节都不变。用户 2026-10-04 的疑问「我都搞到 10 步长
#   了还这么重吗」就是混淆了这两件事。
#
# 0 = 关掉冷却（当前值）。
QWEN_COOLDOWN = float(os.getenv("QWEN_COOLDOWN", "0"))

# ─── 停用的生图渠道（硬件跑不动，不是配置问题）──────
#
# 逗号分隔的 skill 名；**留空 = 全部可用（当前就是空的）**。被列进来的渠道
# **代码全保留**，只是 `generate_image` 不再放行——模型点名也没用，会拿到一句
# 能直接转述的错话。要临时停掉某个渠道，在这里（或 .env）写上它的名字即可。
#
# ⚠️ **2026-10-01 用户拍板全部解封，本列表清空**：qwen_image_v1 与 krea2 都放开。
# 下面是各渠道的来历与风险，保留备查：
#
# ⚠️ 2026-10-01 起把**尺寸档**独立成 3 档，跟 4 个画风正交，凑成 16 个生图渠道：
# 普通档 `anima_<画风>`（728~768×1024，默认不放大）+ 大图档 `hd_fast_<画风>`
# （1024×1536，不放大，最快）/ `hd_2_<画风>`（1328×2000，1.3× 放大，中间档）/
# `hd_3_<画风>`（1536×2304，1.5× 放大，最大最慢、最吃显存）。hd_* 三档**比普通档
# 重得多**（`hd_2` 起就明显慢、`hd_3` 最吃显存），还没经过长期稳定性验证——
# 显存吃紧时优先走普通档。
#
# **qwen_image_v1**（2026-10-01 解封）：
# 2026-09-27 停用它，根因是**内存不足**——一套权重 10.5GB（文本编码器 6018MB +
# unet 4487MB），当时本机只有 16GB 内存，单张就能把整机拖崩（实测五次连崩、
# 三次整机重启；最后一次 17:42:05 入队、17:43:18 重启，日志停在第一张图提交后，
# 队列里**只排了它一张**）。当时外挂启动参数（`--vram-headroom` /
# `--disable-pinned-memory` / `--lowvram`）与更低量化都试到底并被推翻。
#
# ⚠️ 解封当天实测本机总内存**仍是 16GB**（`Win32_PhysicalMemory` 只有一条
# 16GiB，`TotalVisibleMemorySize` 也是 15.62GB），不是早先文档里写的 32GB。
# 用户知悉这个出入，仍拍板「照常解封、风险自担」。也就是说 qwen 的**内存风险
# 并没有真正解除**，只是用户选择接受——真跑崩了别当成新问题。
# 队列侧那套软保护**原样保留**（`skill_priority` 权重 5 排最后 + `QWEN_COOLDOWN`
# 冷却窗，见 image_jobs 的「重渠道优先度」）——它们只管调度顺序，不影响能不能跑。
#
# **krea2**（2026-10-01 解封）：2026-09-27 曾停用，合并后权重 7521.96 MB，比
# anima 重得多，出图还要经 2x Ani4K 放大到 2048×3072（当时的输出尺寸）。
# **没有崩机记录**——当初是「跑不动 / 不稳」的判断，不是像 qwen 那样实测必崩，
# 别把 qwen 那套结论套到它头上。2026-10-01 用户一并拍板解封。
# 同日按用户要求**去掉超分**：画布 1024×1536 → 832×1216，`UpscaleModelLoader` +
# `ImageUpscaleWithModel` 两个节点从工作流里删掉，现在单段直出 832×1216
# （跟 qwen / SD 一个尺寸）。"本机最大输出"这个说法已经作废——最大的是 `hd_3_*`
# （1536×2304）。
#
# **image_gen_v1（SD / SDXL）**：2026-10-01 同日写进工具描述、并加进 QQ 白名单，
# 跟 `agents/draw` 共用。832×1216 单段 30 步，它跟 krea2 都只算**普通权重**（1）。
#
# 停用渠道**代码全留**，只是不再放行；要重新打开：把这里清空即可。
DISABLED_IMAGE_SKILLS = [s.strip() for s in
                         os.getenv("DISABLED_IMAGE_SKILLS", "").split(",")
                         if s.strip()]

# ─── NovelAI（群主独立生图 token）────────────────────
# NAI 是一个**云端**动漫生图 API，跟本机 ComfyUI 完全独立：token 是群主自己的，
# 只在管理员为指定群开通后才可用（见 app/agents.nai_allowed），不共享给别的群。
#
# 为什么单独配代理：本机常驻 Clash 把代理写进了**注册表**，而项目里所有出网口
# 一律 trust_env=False（为了 localhost 的 ComfyUI 不被劫持）。NAI 是外网，必须
# 走代理——这里单独读注册表 / 显式代理，不跟 ComfyUI 那套混在一起。
#
# NAI_API_KEY：群主给的持久 token，只放 .env（已在 .gitignore），绝不进仓库。
# NAI_PROXY：显式代理地址；留空 = 自动读本机注册表系统代理（用户开 VPN 后写在这）。
# NAI_ENABLED：.env 级的硬总闸（默认开），纯紧急熔断用——真正控制「开不开」的
#   是管理页 settings.json 的 nai_enabled（默认关）+ 单群白名单 nai_groups。
# NAI_OPEN_ACCESS：.env 级的使用范围开关。开启后跳过群/私聊白名单，仍受
#   NAI_ENABLED、agent 的 nai_enabled、API Key、额度和队列限制约束。
NAI_API_KEY = os.getenv("NAI_API_KEY", "")
NAI_PROXY = os.getenv("NAI_PROXY", "")
NAI_ENABLED = os.getenv("NAI_ENABLED", "true").lower() not in ("0", "false", "no", "")
NAI_OPEN_ACCESS = os.getenv("NAI_OPEN_ACCESS", "true").lower() not in ("0", "false", "no", "")

# ─── 生图审核（出图前最后一道闸）────────────────────
# 单张的识图超时。**故意比 VISION_TIMEOUT 短**：
# 识图预处理卡住只是让这轮对话慢，而审核卡住是**挡在发图这一步前面**，
# 用户在群里干等。实测正常 1.3~2.1 秒返回，30 秒足够宽松。注意口径已改成
# fail-closed：超时是**拦下不发**（见那个模块的注释），所以这个值往下砍等于
# 更容易误拦——识图偶尔慢一点，本来合规的图也发不出去。别乱调短。
IMAGE_AUDIT_TIMEOUT = float(os.getenv("IMAGE_AUDIT_TIMEOUT", "30"))

# 自定义审核提示词的长度上限（字符）。审核提示词**每张图都要重发一遍**，
# 它是尾巴的一部分、每次都全价计费，所以给个上限防手滑贴进来一整篇文章。
# 内置默认那份约 300 字。2026-10-06 用户从 2000 放宽到 **3000**：实际那份
# 口径已经写到 1961 字、只剩 39 字余量，改一个字就存不进去了。
# ——别再往下砍：明细口径写不下 = 误拦变多，而误拦现在有人二审兜底
# （见人工审核队列），比少烧几十 token 值钱得多。
IMAGE_AUDIT_PROMPT_MAX = int(os.getenv("IMAGE_AUDIT_PROMPT_MAX", "3000"))


__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]
