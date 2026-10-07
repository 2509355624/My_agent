"""ComfyUI 生图工具"""

import os
import logging
import re

import requests
import json
import random
from flask import request
from app import comfy_src, image_jobs
from app import char_guard
from app import nai as nai_mod
from app.cancel import Cancelled, is_cancelled
from app.config import COMFYUI_URL, DISABLED_IMAGE_SKILLS, QQ_AGENT_ID
from app.skills import load_skill, load_workflow

log = logging.getLogger("generate_image")


# 支持图生图（垫图）的 skill。本机有**两种图生图，机制不一样，别当成一件事**：
#
#   ① **重绘** = 动漫 12 档（`anima_*` / `hd_fast_*` / `hd_2_*`）。
#      `LoadImage → VAEEncode` 把源图编成 latent，一段 KSampler 在**它上面**
#      按 denoise 0.6 重新采样：构图大致还在，但整张画面都是重画的。
#      「照这张的姿势画一张动漫版」「垫它当参考」是这条路。
#   ② **编辑** = `qwen_image_v1`（2026-10-02 开放）。
#      源图**不进 latent**，而是当参考图送进 `TextEncodeQwenImage21`——一路进
#      视觉 token（模型「看见」这张图），一路进 conditioning 的
#      `reference_latents`（按位置拼进序列）。KSampler 吃的是那个节点吐出来的
#      **空 latent**，所以 denoise **写死 1**：它是「从零采样、但被参考图钉住」，
#      给它 0.6 等于在一个全零 latent 上减半噪声，位置信息直接乱掉。
#      「把外套换成红色，其余不动」这种**只改一处**的请求是它最强，
#      动漫档的重绘做不到这个精度。**但它是慢渠道**：一套权重 10.5GB，实测一张
#      要 1~2 分钟（anime 20~30 秒），所以**它不是图生图的默认渠道**——
#      2026-10-02 用户明确：「不要让 AI 默认就用 qwen，太耗时」。
#
# 该不该垫图（传不传 `source_image`）**由 AI 判**，代码不设关键词闸
# （2026-10-06 用户拍板：原先那三处「扫本轮原话找图生图/垫图/改图/重绘」的
# 硬编码全删——`_i2i_gate` 拒收、`_i2i_force` 反向补垫图、`direct_gen._i2i_intent`
# 路由，一份词表三个消费点。用户原话：「没有兜底没有问题，大模型自己就知道
# 怎么去做，我们不需要代码去给它硬兜底」。他早就不跑本地 9B，改走 API 大模型，
# 当年「小模型一看见引用图就想改图」那个病根已经不成立）。
# 于是这条边界**只写在文案里**，共三处，改行为要三处一起改：
#   1. 本文件末尾的工具描述与 `source_image` / `prompt` 参数说明；
#   2. `agent_prompt._TOOL_HINTS` 的 generate_image 条目；
#   3. `direct_gen._MASTER_TEMPLATE` / `_REVISE_TEMPLATE` 的【图生图】段。
# 文案口径（用户原话）：**只有他明说「qwen 图生图 + 需求」才填 `source_image`；
# 引用一张图本身不是垫图要求**——引用图 + 说需求 = 拿那张图的提示词改一改重画。
#
# ⚠️ 动漫档（`anima_*` / `hd_*`）的**重绘**已从文案里撤掉：不是后端关了
# （`_I2I_SKILLS` 照旧放行，真传了照样能跑），而是用户口径「Anima 改图最差劲，
# 图生图只要 qwen 一条途径」。别再往提示词里写回「照那张的构图重画用动漫档」。
#
# **hd_3_* 不给**：三档本身就贵（1.5× + 二段 10 步，实测纯执行 ~150 秒），叠上
# 图生图要两分半以上。注意这跟图生图的开销无关——图生图只比文生图多一次
# VAEEncode（实测 +3~6 秒，见 `_make_i2i_workflows.py`），是三档自己贵。
#
# ① 动漫重绘的骨架（`workflow_i2i.json`）由 `_make_i2i_workflows.py` 从同目录的
# 文生图骨架生成：删 EmptyLatentImage、加 LoadImage + VAEEncode、一段 denoise
# 换占位符。二段的放大倍率照抄不动——所以出图尺寸是「源图缩到本档画布长边」再乘它。
_I2I_TIERS = ("anima", "hd_fast", "hd_2")   # hd_3 不给图生图
_I2I_STYLES = ("clear", "soft", "gloss", "curvy")
_I2I_ANIMA_SKILLS = tuple(t + "_" + s for t in _I2I_TIERS for s in _I2I_STYLES)
# ② qwen 的编辑式改图：骨架是**手写**的 `skills/qwen_image_v1/workflow_i2i.json`
# （跟上面那个生成脚本无关，机制完全不同），denoise 定死在文件里 = 1。
_I2I_SKILLS = _I2I_ANIMA_SKILLS + ("qwen_image_v1",)

# ① 一段的重绘强度。**不给模型调**——它一调就会以为「调低 = 只微调」，
# 而对方要的是「照这张重画一张」。0.6 是实测既保得住构图、又出得来细节的位置。
# （qwen 那条不吃这个数：它的 latent 是空的，denoise 只能是 1，见上。）
I2I_DENOISE = 0.6

# ─── 图生图的触发判据：交 AI，代码不判（2026-10-06 拆闸）────────
#
# 这里原先有一整套「扫本轮原话找机制词」的硬编码，三个消费点全部已删：
#   `_I2I_EXPLICIT_RE`（图生图|i2i|垫|改图|修图|p图|重绘…）
#   `_I2I_ASKING_RE`（在问功能 ≠ 在下命令）
#   `_I2I_NO_INTENT_NOTE`（拒收话术）
#   `_i2i_gate`（模型传了 source_image 但原话没命中 → 拒）
#   `_i2i_force`（原话命中但模型没传 → 代码抢在模型前面补上垫图）
# 删的原话依据：「这个硬编码给我去掉，改成让 AI 自己判断，AI 知道怎么判断的」
# 「没有兜底没有问题，大模型自己就知道怎么去做」。
# 判据现在只活在上面列的那三份文案里。历史上留着它们是因为本地 9B 执行不了
# 负向指令（实测「不要图生图」两句里一句照样传 source_image），而那个模型
# 已经不用了。
# 同族但**你没点名、我留着没动**的两道：`_hd_tier_guard`（原话没有档位依据
# 却传了 hd_* → 降回默认档）、`_t2i_guard`（明说「文生图」却传了 source_image
# → 去掉垫图）。要一起拆的话说一声。


# ── 尺寸档守卫（2026-10-04）────────────────────────────────────────
# 「高清」在中文里是两个意思：**带「档」是档位名**（用户从 ComfyUI 导工作流时
# 就是这么叫的：高清快档 / 高清二档 / 高清三档），**不带「档」是画质形容词**
# （高清一点 / 清晰 / 画质好），后者要的是「别糊」而不是「要更大尺寸」。
#
# 为什么要有这层：实测 9B（qwen3.8-9b-heretic）**只学得会正向映射**——
# 描述里写「高清二档 → hd_2_*」它记住了，但「只说了画质形容词就不传 skill」
# 这条**负向规则它执行不了**：7 个 case 各跑 2 遍，光说「高清」的两个都传了
# hd_fast/hd_2，「清晰点」传了 hd_fast。而这条判据是确定性的（有没有「档」字
# / 有没有像素数字），拿代码判一次比指望小模型记住可靠。
# 跟上面拆掉的那几道同一个证据源（qq_api 的本轮原话），网页端没有原话 → 不拦。
_HD_TER_RE = re.compile(
    r"(?:高清|超清)?\s*"
    r"(快\s*档|一\s*档|二\s*档|三\s*档|1档|2档|3档)"        # 档位名
    r"|(?:1\d{3}|2\d{3})\s*[x×*]\s*(?:1\d{3}|2\d{3})"        # 1328×2000
    r"|(?:当|做|作)\s*壁纸|竖屏长图|要最大",                  # 明确用途
)
_HD_QUALITY_ONLY_RE = re.compile(
    r"高清|清晰|清楚|画质|精细|高清度|不糊|别糊|别太糊|太糊|模糊|模糊点")


def _hd_tier_guard(skill, default_skill):
    """模型传了 hd_* 档，但对方原话里没有档位依据 → 降回默认档，返回 (skill, note)。

    只在 QQ 会话轮里判（拿不到原话时一律放行：没证据源就别拦）。
    **只降不升**：对方明明报了「二档」而模型没传 skill，不在这里补——补了就是
    代码替模型猜用户意图，而"报了档却没传"在实测里没出现过（9B 恰恰相反，
    是见到「高清」就乱传）。真要补也不能猜画风，只能等模型自己传。
    """
    if not skill or not skill.startswith("hd_"):
        return skill, ""
    from app import qq_api

    text = qq_api.current_turn_text()
    if text is None:                      # 网页端 / 单测：无证据，不拦
        return skill, ""
    if _HD_TER_RE.search(text):
        return skill, ""                   # 有档位依据，放行
    if not _HD_QUALITY_ONLY_RE.search(text):
        return skill, ""                   # 连画质词都没提，判不准，别乱动
    log.info("尺寸档降级：原话只有画质形容词（%r），不带 skill 参数降回 %s",
             text[:60], default_skill)
    # 把原话里**实际命中的那个词**回给对方，别只说「这类词」——
    # 他得知道到底是哪个词让系统换了渠道，下次才好改口。
    hit = _HD_QUALITY_ONLY_RE.search(text).group(0)
    return default_skill, (
        "（系统已自动调整：对方原话里的「%s」只是画质形容词、没报任何档位名或"
        "具体像素，所以没用 %s，已按默认的 %s 出图。对方真要更大尺寸，"
        "下次让他直接说「二档」「三档」或报像素。）" % (hit, skill, default_skill)
    )


# ── 文生图守卫（2026-10-04）────────────────────────────────────────
# 「明说了文生图就不许垫图」。它和 `_hd_tier_guard` 是同族（都扫本轮原话），
# 2026-10-06 拆图生图硬闸时**用户没点名这两条，所以留着**——要一起拆再说。
#
# 当初加它的实测依据（本地 qwen3.8-9b-heretic，2 遍）：对方说「不要图生图了，
# 文生图」，9B 两次里一次传了 `source_image`。跟 `_hd_tier_guard` 同一个病根
# ——**负向指令它执行不了**（「别垫图」/「不要图生图」都是负向），而正向映射
# 「文生图 = 不传 source_image」它学得会。与其指望它记住禁令，不如代码判。
_T2I_ONLY_RE = re.compile(
    r"文生图|纯文生"
    r"|不(?:要|用|走)?(?:图生图|垫图|参照图|参考图)"
    r"|别(?:用)?(?:图生图|垫图|参照图|参考图)"
    r"|重新生成|重画|从头画|重新画"
)

# 刻意**不带外层括号**：回执那边会用「（垫图：%s）」把它括起来，套两层
# 就变成「（（…））」。同理别用句号收尾——拼接处本来就有。
_T2I_FORCED_NOTE = (
    "系统已自动调整：对方本轮明说了「文生图」，所以没垫图、没参照他发的那张，"
    "按他自己描述 / 反推的提示词重新画了一张"
)


def _t2i_guard(is_i2i):
    """对方明说「文生图」而模型却传了 source_image → 挡掉垫图，放行文生图。

    返回 (是否继续, 给回执的说明)。与 `_hd_tier_guard` 一样只在 QQ 轮里判
    （`current_turn_text()` 为 None = 网页端/单测，没有原话这个证据源，不拦）。
    """
    if not is_i2i:
        return True, ""
    from app import qq_api

    text = qq_api.current_turn_text()
    if text is None:
        return True, ""
    m = _T2I_ONLY_RE.search(text)
    if not m:
        return True, ""
    log.info("去掉垫图：本轮原话明说了「%s」，按文生图重画", m.group(0))
    return True, _T2I_FORCED_NOTE


# ── 「要提示词守卫」已删除（2026-10-06）──────────────────────────────
#
# 这里原先是 `_prompt_ask_guard` + `_PROMPT_NOUN_RE` / `_PROMPT_ASK_RE` /
# `_PROMPT_ASK_NOTE`：原话命中「提示词/种子 + 给我/是什么/停」就把整单生图
# 拒掉，指路 recall_image。立它的起因是 2026-10-04 实测「这个的提示词是
# 什么」连烧 3 张额度。
#
# 但它只认关键词、不认意图，误拦比它治的病更碍事——233 群实录：「引用这张图，
# 我要她抓手的手势，角色换成花火」是**改图请求**，只因正文带「提示词」三个
# 字就被整单拒掉。用户 2026-10-06 拍板：
#   「我们加了太多这种硬编码的规则了，导致 AI 它变得特别傻…能就我们让 AI
#     来做的，我们就直接让 AI 来做，不要为了省那几毛钱，那几千 token 就搞得
#     那么复杂」
# 判意图这件事本来就该模型做：要词条它会调 recall_image，要新图它就画。
# 别再把这条加回来——要治「白烧额度」就把 `recall_image` 的工具描述写清楚，
# 别在代码里猜对方想干嘛。


# 没点名 skill 时的文生图默认渠道。
#
# 2026-09-30 20:3x 起：**本机只剩 4 个动漫渠道，SD 渠道（image_gen_v1）已归档。**
# （2026-10-01 用户又拍板把 SD 保留回来——`skills/image_gen_v1/` 留在原地、
# `agents/draw/agent.json` 白名单里有它，所以它**又是可用渠道**了。
# 同日补齐：工具描述里已经写上它（Anima 家族 16 个 + qwen + image_gen_v1 + krea2 +
# nai），QQ 白名单也放了它和 krea2——上面那条「已知的遗留不一致」结案。
# 2026-10-02 又加了 `nffa`（Illustrious 系画风 + 手/脸两段修复，一张 40~75 秒，
# 用户点名才走）；2026-10-03 再加 `nai_wide`（NAI 横版 1216×832，跟 `nai` 竖版
# 共用同一套闸 / 额度 / 队列，只是文生图的构图方向不同）：可传名字现在是
# 16 + qwen + image_gen_v1 + krea2 + nffa + nai + nai_wide = 22 个。）
# 四个都由用户当天的 ComfyUI 工作流直接转来，共用同一套两段采样骨架，
# **差别在底模组合，表现为画风差异**——所以渠道名按**视觉特征**取，
# 模型看到名字就能联想效果（用户要求「形象的命名，这样有辨识度」）：
#
#   - `anima_clear`：清透素净、光最平。realskin → realskin（同一块底模）。728×1024。
#   - `anima_soft`：柔光哑光素肌。realskin → reality。728×1024。
#   - `anima_gloss`：冷调油光亮面。reality → realskin。768×1024 → 1.1× → 848×1128。
#   - `anima_curvy`：丰腴强光影。harem → reality。728×1024。
#
# 2026-10-01 先加了**三个尺寸渠道**（用户从 ComfyUI 导的 `高清快挡` / `高清二档` /
# `高清三档`），随后扩成**画风 × 尺寸档**的完整交叉，共 16 个：
#   - 画风 4 种：clear / soft / gloss / curvy（就是上面那 4 套底模组合）；
#   - 尺寸档 4 档：普通档 `anima_<画风>`（画布 728~768×1024）+ 三个高清档
#     `hd_fast_<画风>` / `hd_2_<画风>` / `hd_3_<画风>`（画布都是 1024×1536）。
# **画风和尺寸是两维正交的**，不再是「一类渠道」。
#
# 高清档画布 1024×1536 → latent 128×192，输出 = round(latent × scale_by) × 8：
#   - `hd_fast_*`：放大 1×   → **1024×1536**。步数和常规渠道一样（10/5），**速度差不多**。
#   - `hd_2_*`：   放大 1.3× → **1328×2000**（round(166.4)=166、round(249.6)=250）。中间档。
#   - `hd_3_*`：   放大 1.5× → **1536×2304**。本机最大也最慢（二段 10 步），更吃显存。
#
# 12 个高清渠道由 `_make_hd_channels.py` 从「尺寸骨架 × 底模组合」生成，
# **不要手工改其中某一份 workflow.json** —— 手改会让 16 个渠道之间悄悄不一致。
# ⚠️ 高清档的正向模板是 `@kibro, __MULTI_PROMPTS__`，但用户存的工作流里
# **没有 `@kibro`**（ComfyUI 存的是当时那张图的完整词），所以**重导尺寸骨架**时
# 必须显式传 `--prefix "@kibro, "`，否则脚本识别不出前缀、画风会不对。
#
# 命名依据是**同种子两轮实测**（`_compare_channels.py`，对比图见
# `D:\AI\ComfyUI\output\_channel_compare.png`）：两轮特征一致 → 是模型特征不是种子运气。
# 最硬的一条是 `anima_curvy` 的体型差异，两轮都明显。
#
# ⚠️ 默认渠道换过四次（`anima` → `anima_realskin` → `anima_soft` → `anima_clear`
# → **`silver`**，2026-10-06
# 用户拍板：「我们默认渠道就是 sILVR，把它做成默认渠道就行了」——**不做管理页
# 的默认渠道下拉**，写死在代码里），
# **每次换都会连累一批写死名字的地方**（工具描述、拒收话术、4 个 skill.md 的「默认」标记、
# `comfy_workflow.py` 的默认参数、以及一批测试）。所以：
#   - 下面这个常量是**唯一真相源**，话术/描述里要提默认渠道名一律 `%` 它，别写字面量；
#   - 换默认渠道时按 `tests/test_image_channels.py::DefaultChannelTest` 的报错清单挨个改。
#
# 工作流都直接取自用户 ComfyUI 里导出的文件——他每调一次就要重导一次，
# 步数/CFG/采样器/放大倍率**都是他随手调的旋钮，别把这里的数字当契约**。
# 历史：更早有过「单底模 anima」「双底模 anima_2」「anima_realskin / anima 两个渠道」
# 等阶段，都已随本次切换归档到 `skills/_archive_20260930/`。
T2I_DEFAULT_SKILL = "silver"


# ─── QQ 侧生图开关 ───────────────────────────────────

def _qq_gate():
    """QQ 会话里的生图总闸 + 单群闸 + **私聊每日额度闸**；网页端不受限。

    靠 qq_api 的线程本地绑定知道「此刻在为哪个 QQ 会话服务」。管理页
    关掉后下一轮就生效（settings.json 走 mtime 缓存），不用重启。拒绝
    时返回一句模型能转述的话，而不是抛错——让它正常回话「生图被关了」，
    别让整轮变成工具执行失败。
    """
    from app import qq_api
    from app.agents import image_gen_allowed, image_quota_allowed
    target, target_id = qq_api.current_context()
    if target is None:
        return None
    ok, why = image_gen_allowed(QQ_AGENT_ID, target, target_id)
    if not ok:
        return ("错误：" + why
                + "，本次不生成图片。别再重试，"
                  "直接告诉对方现在画不了。")
    # 私聊每日额度（群聊恒放行，见 agents.image_quota_allowed）。文案和上面那条
    # 刻意不同：这条的出路是「明天再来」，说成「现在画不了」会让对方以为机器坏了
    # 而反复重试。
    ok, why = image_quota_allowed(QQ_AGENT_ID, target, target_id)
    if not ok:
        # 「这一轮」这个限定词别删（2026-10-01 用户报「加了白名单 AI 还说我
        # 限额」）：原来写的是「别再重试」，模型读成**永久**禁令，之后哪怕
        # 管理员已经把人加进免额名单，它也不再调一次工具确认，只照着这段
        # 旧拒绝回话。限死本轮即可——额度状态另有每轮的
        # `agents.image_quota_line` 兜底。
        return ("错误：" + why
                + "，本次不生成图片。这一轮别再重试，也别说「稍等」「马上好」，"
                  "直接告诉对方今天的额度用完了、明天再来；"
                  "对方问起就说这是私聊的每日限制。")
    return None


def _charge_quota(job, target, target_id):
    """接单成功 → 扣一个私聊额度名额，并在 job 上打标记；返回一句实时余额。

    扣额放在「真正接单之后」（用户 2026-09-30 选的方案）：这样拒收（队列满、
    渠道停用、ComfyUI 没在线）一张都不扣，而**连点刷队列**又拦得住——额度在
    提交那一刻就占了。

    `job.quota_charged` 是「这张到底扣没扣」的唯一凭据：worker 的 `_finish`
    只认它，不靠自己重新推断（推不出来——它看不见这次是私聊还是群聊之外的信息）。

    返回值是给提交回执拼尾巴用的（见 `_quota_balance`），不该说话时为空串。
    调用方都把它接上；单元测试直接调这个函数、忽略返回值也没问题。
    """
    if target != "private":
        return ""
    from app import image_quota
    n = image_quota.charge(target_id)
    job.quota_charged = True
    log.info("私聊生图额度：%s 今日已用 %d 张", target_id, n)
    return _quota_balance(target_id, n)


def _quota_balance(target_id, used):
    """刚扣完额度后那句「现在还剩几张」，拼进提交回执。

    为什么额度行（`agents.image_quota_line`）还不够：它一轮**只算一次**，算的
    是本轮开跑之前的数。同一轮里连画两张时模型看到的还是那个旧数，而额度闸
    每次调工具都现读账本——「行说还能画 3 张、工具却拒了」就是这么来的。扣完
    当下把数写进回执，模型手里就有了和闸同源的最新值。

    限流关掉 / 免额名单里时不返回：那种情况下没数可报，硬加一句「不限量」
    只会挤掉真正要说的那句「画好会自动发」。
    """
    from app.agents import (private_image_daily_limit,
                            private_image_quota_whitelist)
    limit = private_image_daily_limit(QQ_AGENT_ID)
    if limit <= 0 or str(target_id) in private_image_quota_whitelist(QQ_AGENT_ID):
        return ""
    return ("（今天私聊额度 %d/%d，**剩 %d 张**，这是刚扣完的实时数，"
            "别照本轮开头那行额度说。）" % (used, limit, max(0, limit - used)))


# ─── 提交回执：后台直发（2026-10-04 用户拍板）─────────────
#
# 用户原话：「AI 提交任务之后直接发一个回执，就说任务已经提交、前面还有 XX 在
# 排队，这个是**直接发的、不是经过 AI**——它老是瞎编东西，我要的是最直接的来自
# 后台的回执。」张数是 `image_jobs.ahead_of` 现算的，本来是真的；坏就坏在它得
# 经模型那张嘴转述一遍 —— 一转述就编。所以提交成功那一刻**工具自己发**。
#
# 只直发 QQ 会话（`target is not None`）：网页端本来就要等出图，没有这条回执。
# 直发成功的回执以 `image_jobs.RECEIPT_SENT_MARK` 开头，agent.py 的掐断判据与
# qq_bot 的「本轮别再采纳模型正文」都认它（格式只有一个真相源，别在这儿重写）。
_RECEIPT_QUEUED = "任务已提交，前面还有 %d 张在排队。"
_RECEIPT_RUNNING = "任务已提交，正在画了。"
# 第二行报**这一张走的哪条渠道**（2026-10-06 用户拍板：「提交任务的时候，可以
# 说明生图的渠道是什么」）。渠道 id 是我们自己起的名字，原样报——它同时是
# 「渠道名 + 你的需求」那条用法的示范，用户看着这行就知道下次怎么点名。
_RECEIPT_CHANNEL = "当前渠道：%s"
# 附言的内置默认（管理页 `receipt_note` 一整段可改掉，见 agents.receipt_note）。
# 刻意短：这是刷屏的系统回执，不是使用手册——全部渠道另有 /更多渠道。
_RECEIPT_NOTE_DEFAULT = (
    "想换渠道就发「渠道名 + 你的需求」（例：jank 银发初音未来）。\n"
    "常用：silver（默认）/ 快档 / 二档 / 三档 / qwen / nai / jank\n"
    "全部渠道：@%s /更多渠道"
)


def receipt_note_default():
    """内置的提交回执附言（把机器人名字填进去）。管理页拿它当编辑框初始内容。"""
    from app.config import QQ_BOT_NAME
    return _RECEIPT_NOTE_DEFAULT % QQ_BOT_NAME


def _receipt_text(ahead, skill=None):
    """后台直发的回执正文：短、纯状态、像系统回执（不给模型留编的余地）。

    三段拼起来：状态句 → 当前渠道 → 附言（管理页可编辑，没设就用内置默认）。
    `skill` 为空（老调用方 / 手工构造的 Job）就少报一行，不硬编一个渠道名上去。
    """
    from app import agents as agent_store
    lines = [_RECEIPT_QUEUED % ahead if ahead > 0 else _RECEIPT_RUNNING]
    if skill:
        lines.append(_RECEIPT_CHANNEL % skill)
    note = agent_store.receipt_note(QQ_AGENT_ID)
    if note is None:
        note = receipt_note_default()          # 没设过 = 用内置那份
    if note:
        lines.append(note)
    return "\n".join(lines)


def _send_receipt(target, target_id, receipt):
    """把回执**直接**发进会话；发成功返回 True。

    发不出去（适配层掉线、私聊非好友）返回 False——调用方退回老文案让模型
    转述，**绝不在这儿假装发过**：那正是这次要修的毛病。
    """
    try:
        image_jobs._send_text(target, target_id, receipt)
    except Exception:
        log.exception("生图回执直发失败 %s %s", target, target_id)
        return False
    return True


def _qq_receipt(job, target, target_id, source_note="", quota_tail=""):
    """QQ 侧提交成功后的回执（本机 / NAI 两条提交路径共用）。

    能直发就直发——返回以 `image_jobs.RECEIPT_SENT_MARK` 开头的文本，交给
    agent.py 掐断循环、qq_bot 闭嘴；发不出去才退回老文案（由模型转述），
    至少不会整轮一声不响。

    `quota_tail` 是**给模型看的**实时余额（只有私聊且限流时非空），直发那条
    不带它——回执要短、要像系统回执，别把额度说明也念给群里听。
    """
    ahead = image_jobs.ahead_of(job)
    receipt = _receipt_text(ahead, getattr(job, "skill", None))
    tail = (("（垫的是%s。）" % source_note) if source_note else "") + quota_tail
    if _send_receipt(target, target_id, receipt):
        return (image_jobs.RECEIPT_SENT_MARK + receipt + "\n"
                + "（上面那句系统已经直接发到会话里了：**不要再复述排队 / 张数**，"
                  "也不用说「稍等 / 马上好」，本轮别再为这件事说什么。）"
                + tail)
    if ahead > 0:
        return ("已经排上队了（前面还有 %d 张），排到就画，"
                "画好会自动发到群里。"
                "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" % ahead + tail)
    return ("已经在画了，画好会自动发到群里。"
            "不要输出图片地址，也不要说「图在下面 / 稍等」，"
            "直接把想说的话说完就行。" + tail)


# ─── 工具函数 ────────────────────────────────────────

def _parse_loras(lora_str):
    """「名字:强度,名字:强度」-> [(name, strength), ...]。

    格式刻意从简（小模型要写得出来）：强度一个数同时给 model 和 clip。
    写错抛 ValueError，消息里带上原因，让模型能自己纠正。
    """
    specs = []
    for part in lora_str.split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, raw = part.rpartition(":")
        if not sep or not name.strip():
            raise ValueError("lora 参数格式应为「文件名:强度」：" + part)
        try:
            strength = float(raw)
        except ValueError:
            raise ValueError("lora 强度要是数字：" + part)
        specs.append((name.strip(), strength))
    if not specs:
        raise ValueError("lora 参数是空的")
    return specs


def _lora_chain(workflow):
    """按 checkpoint→lora 链的顺序返回 lora 节点 id 列表。

    兼容 LoraLoader（带 clip）和 LoraLoaderModelOnly（krea2 用的，只挂 model）。
    不硬编码节点 id（两个工作流的 id 编号不同），沿 model 输入的连线走：
    第一个槽的 model 来自 checkpoint 加载节点，后面每个槽的 model 来自前一个槽。

    起点识别用**小写包含**：ComfyUI 里同一个加载器有多种拼写——`CheckpointLoaderSimple`
    （image_gen_v1）、`UnetLoaderGGUF`（krea2）、`UNETLoader`（anima）。
    原来写成 `"UnetLoader" in class_type` 是大小写敏感的，`UNETLoader` 全大写**匹配不上**，
    于是 anima 的两个 lora 槽整条链找不到，点名换 lora 会误报「当前工作流没有 lora 槽」。
    """
    loaders = {nid: node for nid, node in workflow.items()
               if node.get("class_type") in ("LoraLoader",
                                             "LoraLoaderModelOnly")}
    sources = {}
    for nid, node in loaders.items():
        src = (node.get("inputs") or {}).get("model")
        sources[nid] = src[0] if isinstance(src, list) and src else None
    ckpts = {nid for nid, node in workflow.items()
             if "checkpointloader" in (node.get("class_type") or "").lower()
             or "unetloader" in (node.get("class_type") or "").lower()}
    chain, current = [], next(
        (nid for nid, src in sources.items() if src in ckpts), None)
    while current is not None and current not in chain:
        chain.append(current)
        current = next((nid for nid, src in sources.items()
                        if src == current and nid not in chain), None)
    return chain


def _available_loras():
    """从 ComfyUI 实时拉 lora 清单；拿不到返回 None（不拦截，交给 ComfyUI 自己拒）。

    清单不塞进工具描述——几十个文件名每轮都发不值当，只在写错时才拿来救场。
    """
    try:
        resp = requests.get(COMFYUI_URL + "/object_info/LoraLoader", timeout=10)
        resp.raise_for_status()
        return list(resp.json()["LoraLoader"]["input"]["required"]["lora_name"][0])
    except Exception:
        return None


def _apply_loras(workflow, lora_str):
    """把 AI 指定的 lora 填进槽位。传了就**完全接管**：没填满的槽强度归零，
    工作流里默认那组 lora 不再掺和——避免「指定了角色 lora 但饱和度修正
    还在捣乱」的混搭怪相。出错返回模型能转述的一句话，成功返回 None。
    """
    try:
        specs = _parse_loras(lora_str)
    except ValueError as e:
        return str(e)
    names = _available_loras()
    if names:
        bad = [n for n, _ in specs if n not in names]
        if bad:
            return ("错误: 这些 lora 不存在: " + ", ".join(bad)
                    + "。可用 lora: " + ", ".join(names))
    chain = _lora_chain(workflow)
    if not chain:
        return "错误: 当前工作流没有 lora 槽，去掉 lora 参数用默认的画就行"
    specs = specs[:len(chain)]          # 传多了按槽位截断，不报错
    for i, nid in enumerate(chain):
        inputs = workflow[nid]["inputs"]
        model_only = workflow[nid]["class_type"] == "LoraLoaderModelOnly"
        if i < len(specs):
            name, strength = specs[i]
            inputs["lora_name"] = name
            inputs["strength_model"] = strength
            if not model_only:
                inputs["strength_clip"] = strength
        else:
            # 闲槽等效关闭：强度归零（ModelOnly 没有 clip 输入，别塞进去，
            # 否则 ComfyUI 会报未知输入）
            inputs["strength_model"] = 0.0
            if not model_only:
                inputs["strength_clip"] = 0.0
    return None


def _intent_key(prompt, skill, lora, seed=None):
    """一次生图请求的「意图指纹」：模型给的那几样原始参数。

    不能拿 workflow 当指纹——不点名种子时里面每次都填随机 seed，两次一模一样
    的请求也会算出两个不同的值，查重就永远不中。空值归一掉：skill 不传和传
    "" 在下游是同一件事。

    `seed` **只在对方点名种子时才并进指纹**（调用方不点名就传 None）：点名
    种子的那次提交是「拿这个数再画一张」，跟同时发出的随机提交根本不是一回事，
    不该被重复提交守卫吞掉；而两次**同种子同提示词**的连点仍然算重复。
    """
    bits = [str(prompt or ""), str(skill or ""), str(lora or "")]
    if seed is not None:
        bits.append(str(seed))
    return json.dumps(bits, ensure_ascii=False)


SEED_MAX = 2 ** 32 - 1


def _resolve_seed(raw):
    """把模型/对方给的种子折成一个真种子。返回 (seed, 点名了吗, 错话)。

    为什么**越界报错而不是钳位**：种子是用来复现的。对方说「用 42 号种子」，
    钳到别的数就得到一张对不上的图，他还以为是自己记错了——一句错话比一张
    悄悄不同的图有用得多。

    为什么要认字符串：模型填参数时 `seed: "1234567890"` 和 `seed: 1234567890`
    都常见（`execute_tool` 是 `fn(**args)`，不做类型清洗）。

    `True`/`False` 单独挡：bool 是 int 的子类，不挡就会把「True」当成种子 1
    悄悄画一张（同 `private_image_quota` 那个接口里对 True 的处理）。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return random.randint(1, SEED_MAX), False, ""
    if isinstance(raw, bool):
        return None, False, ("错误：seed 得是个数字（0 ~ %d），不能是 true/false。"
                             "不知道种子是多少就别传这个参数。" % SEED_MAX)
    if isinstance(raw, str):
        text = raw.strip().replace("_", "").replace(",", "").replace(" ", "")
        try:
            value = int(text)
        except ValueError:
            return None, False, ("错误：seed「%s」不是一个数字。种子就是图号旁边"
                                 "那串纯数字，照原样填；不确定就别传，系统会随机。"
                                 % raw)
    elif isinstance(raw, int):
        value = raw
    else:                       # float / dict / 其它模型现编的东西
        return None, False, ("错误：seed 得是 0 ~ %d 的整数，不能是 %r。"
                             "不确定就别传这个参数。" % (SEED_MAX, raw))
    if not 0 <= value <= SEED_MAX:
        return None, False, ("错误：种子 %d 超出范围（只能是 0 ~ %d 的整数）。"
                             "对方给的那个号八成抄错了，跟他再确认一次。"
                             % (value, SEED_MAX))
    return value, True, ""


# 同一件事在还没出图之前又被提交一遍时的回执。不是「拒收」——那张图确实存在，
# 只是不需要第二张，所以措辞必须让模型明白「已经有了」，否则它会以为没提交上，
# 转头又试一次（这正是 2026-10-01 重复出图的老路）。
_DUPLICATE_NOTE = (
    "系统：这次调用**没有执行**——这个会话里已经有一张**参数完全一样**的图还"
    "没出，它就在队列里（见尾巴上的 [最近生图]）。不用再提交，也别跟对方解释"
    "什么「重复」；直接把想说的话说完就收尾，图会自己发到会话里。"
    "对方真要一张不一样的，他会明说。"
)


def _canvas_long_side(workflow):
    """本档画布的长边，读不出来返回 None。

    图生图的出图尺寸 = 「源图缩到画布长边」再乘二段的放大倍率，**跟源图原始尺寸
    无关**。不缩的话：垫一张小图，高清档出来还是小图，「高清」就名不副实；垫一张
    4000×3000 的大图，一段的 latent 会被撑到爆显存。

    画布就写在**文生图骨架**的 `EmptyLatentImage` 里（图生图骨架把它删了，换成
    LoadImage + VAEEncode），所以这里读的是同一个 skill 目录的 `workflow.json`——
    不另立元数据，也就不会跟骨架走散。
    """
    for node in (workflow or {}).values():
        if node.get("class_type") == "EmptyLatentImage":
            ins = node.get("inputs") or {}
            try:
                return max(int(ins.get("width", 0)), int(ins.get("height", 0)))
            except (TypeError, ValueError):
                return None
    return None


def apply_prompt_placeholders(workflow_str, prompt, seed):
    """把提示词和种子填进工作流 JSON 字符串，返回工作流 dict。

    从 `_generate_image` 抽出来（2026-10-05）：随机口令被审核拦下后 worker
    要**同 job 重跑**，重跑时得用同一套规则重建工作流——规则只留这一份，
    两处共用，别让转义规则悄悄分叉。

    替换规则（原样保留，别动）：
    - `__MULTI_PROMPTS__` → 转义后的提示词。可以独占一个 JSON 字符串，也
      可以嵌在更大字符串里（如节点 4 = "@kibro, __MULTI_PROMPTS__"，工作流
      自带固定画风/触发词前缀）。统一按「字符串内部转义替换」处理。
    - `"__SEED__"`（连引号）→ 裸数字。ComfyUI 的 KSampler.seed 是 INT 字段，
      只替内容、留着引号就变成字符串 "123456"，提交时类型校验不过；老写法
      是给自定义节点用的（对 seed 类型不敏感），换标准 KSampler 后必须落成
      真数字。`__SEED__` 裸占位符兜底再替一遍。
    - ⚠️ 动漫系渠道（anima_* / hd_*_*）是**两段采样**，工作流里有两个
      KSampler、两处 `__SEED__`，而这里是**全局替换** ⇒ 一二段拿到同一个数。
      「这张图的种子」始终就是报出去的那一个数。
    """
    prompt_escaped = (prompt.replace('\\', '\\\\').replace('"', '\\"')
                      .replace('\n', '\\n').replace('\r', ''))
    workflow_str = workflow_str.replace("__MULTI_PROMPTS__", prompt_escaped)
    workflow_str = workflow_str.replace('"__SEED__"', str(seed))
    workflow_str = workflow_str.replace("__SEED__", str(seed))  # 兜底：裸占位符
    return json.loads(workflow_str)


def _apply_i2i_placeholders(workflow, uploaded, denoise_txt):
    """图生图专用占位符（只在 workflow_i2i.json 里出现）。"""
    for key, val in (('"__SOURCE_IMAGE__"', json.dumps(uploaded)),
                     ('"__DENOISE__"', denoise_txt)):
        s = json.dumps(workflow).replace(key, val)
        workflow = json.loads(s)
    return workflow


def build_t2i_workflow(skill, prompt, seed):
    """按渠道模板 + 提示词 + 种子构建**文生图**工作流 dict。

    供 worker 的「随机口令被拦静默重抽」用（image_jobs.process）。skill 不
    存在或没有工作流返回 None（重抽路径拿到 None 就放弃重试，走兜底话术）。
    """
    sd = load_skill(skill)
    if not sd or not sd["workflow"]:
        return None
    return apply_prompt_placeholders(json.dumps(sd["workflow"]),
                                     prompt, seed)


def _generate_image(prompt, skill=None, lora=None, source_image="",
                    denoise=None, seed=None, use_character=None,
                    _skip_confirm=False, resample_fn=None):
    # `_skip_confirm`：直达生图管道（app/direct_gen.py）专用——用户打的就是
    # 「渠道+描述」的明确指令，等于已经确认过了，再拦一发确认卡纯属多一轮。
    # agent 路径保持默认 False（拦）。
    # `resample_fn`（2026-10-05）：**随机口令专用**——无参可调用体，被审核拦下
    # 时由 worker 调它换一条新提示词同 job 重跑（静默，不回「未过审」，见
    # image_jobs.process 的拦截分支）。普通生图不传，拦截行为与从前一致。
    # `denoise`：**只有 NAI 图生图**消费它（见下面的 `_nai_strength(denoise)`）。
    # 本机渠道的图生图强度由 `I2I_DENOISE` 定死，不收这个参数——见下面的 i2i 分支。
    # `source_image` 两边都认（本机走 workflow_i2i.json，NAI 走云端）。
    #
    # `seed`：**只有本机渠道认**（NAI 那条分支见下面，它直接拒）。不传 = 系统
    # 随机；传了就把这个数填进工作流，用于「同一个种子、换提示词」再画一张。
    # 校验和「为什么越界报错而不是钳位」写在 `_resolve_seed` 里。
    #
    # `use_character`（2026-09-30 随角色底模机制一起下线）：原义是「用这个 skill
    # 自带的角色底模」，而**只有 image_gen_v1 有 `character.txt`**。SD 归档后
    # 所有动漫渠道（含后来加的尺寸渠道）都没有角色底模，传了也不生效。参数本身已从工具描述和
    # `parameters` 里删掉（模型看不到它），这里留着是**最后一道兜底**：
    # `execute_tool` 是 `fn(**args)`（registry.py:40，不做参数过滤），模型万一
    # 手滑传了它，删掉签名就会抛 TypeError 被包成「工具执行失败」，模型会以为
    # 工具坏了、反复重试。`__CHARACTER__` 占位符的替换则一并删了——没有工作流再用它。

    # 提交前先看一眼：已经中断就别再往 ComfyUI 队列里塞新任务了
    if is_cancelled():
        return "已中断：用户取消了本次生成。"

    gate = _qq_gate()
    if gate is not None:
        return gate

    # ⚠️ 这里原先有一道「要提示词守卫」`_prompt_ask_guard`：原话命中
    # 「提示词/种子 + 给我/是什么/停」就**整单拒掉**，指路 recall_image。
    # 2026-10-06 用户拍板删除——原话：
    #   「我们加了太多这种硬编码的规则了，导致 AI 它变得特别傻…
    #     能就我们让 AI 来做的，我们就直接让 AI 来做，不要为了省那几毛钱」
    # 病根是它只认关键词、不认意图：「引用这张图，我要她抓手的手势，角色换成
    # 花火」明明是改图请求，只因为正文带「提示词」就被整单拒掉（233 群实录）。
    # 现在交给模型自己判：要词条就调 recall_image，要新图就画。

    # 角色点名守卫（2026-10-04）：原话点名角色 A、prompt 写的却是角色 B
    # → 拒回要求整条重写（实测「画纳西妲」抄出整套胡桃模板，正向规则
    # 压不住高频锚定）。放在 NAI 分流前，两条渠道共用。
    refused = char_guard.check(prompt)
    if refused:
        return refused

    # 该不该垫图 = 模型传没传 `source_image`，代码不再扫原话判（2026-10-06 拆掉
    # 了 `_i2i_gate` / `_i2i_force` 这一对，理由见文件开头那段「交 AI 判」）。
    # `is_i2i` 仍在 NAI 分流**之前**算：两条渠道共用同一个 source_image 参数。
    is_i2i = bool(str(source_image or "").strip())

    # 对方**明说了文生图**而模型却传了 source_image → 去掉垫图，按文生图重画。
    # 必须在 `is_i2i` 算出来**之后、`if skill in NAI` 分流之前**：① 改完要重新算
    # is_i2i，下面所有分支都以它为准；② NAI 走云端也是同一个 source_image 参数，
    # 不该漏判。（这条与 `_hd_tier_guard` 同族——都要扫本轮原话，2026-10-06
    # 拆图生图硬闸时用户没点名这两条，**留着没动**。）
    _, t2i_note = _t2i_guard(is_i2i)
    if t2i_note:
        source_image = ""
        is_i2i = False

    # NAI（NovelAI）云端生图：群主独立 token，与 ComfyUI 完全隔离。
    # 不碰下面那套 ComfyUI 探活 / 加载 / skill：它走自己的云分支（见
    # image_jobs._process_nai），token 只给指定群用（app/agents.nai_allowed）。
    # 必须在 ComfyUI 探活之前就分流，否则没开 ComfyUI 的机器会被卡在探活那句。
    # 图生图（垫图）与文生图共用同一套 NAI 闸：nai_allowed 不放行，
    # i2i 也一样进不来——不新增开关。横竖两个渠道（`nai` / `nai_wide`）也共用
    # 同一套闸和同一份额度，没有任何新开关。
    if skill in image_jobs.NAI_SKILLS:
        from app import nai, qq_api
        from app.agents import nai_allowed
        # 种子这事儿到 NAI 门口就停：它是云端出的图，模型版本、参数都在别人
        # 机器上，同一个数在 NAI 那边不保证还是同一张图（用户 2026-10-02 明确
        # 「NAI 加种子无意义」）。但**必须明说**，不能默默忽略——不然对方以为
        # 「同种子换提示词」这套在 NAI 上也成立，照着做就对不上图了。
        if seed is not None and str(seed).strip():
            return ("错误：NAI 是云端出图，种子指定不了（它那边同一个数也不保证"
                    "复现同一张）。要按种子重画只能走本机渠道：anima_* / hd_*_* / "
                    "qwen_image_v1 / image_gen_v1 / krea2 / nffa。")
        target, target_id = qq_api.current_context()
        ok, why = nai_allowed(QQ_AGENT_ID, target, target_id)
        if not ok:
            return ("错误：" + why + "，本次不使用 NAI。"
                    "直接告诉对方现在用不了，别再重试。")
        if target is None:
            return "错误：NAI 仅支持 QQ 使用，网页端用不了。"
        # 垫图：只认本轮引用的图（comfy_src.resolve 的既有契约），取图失败
        # 就实话实说，绝不退回文生图——对方以为改的是自己那张，收到的却是
        # 凭空画的，比直接报错糟得多。base64 和**出图尺寸**在这里一起算好、
        # 快照进队列：worker 线程读不到 qq_api 的线程本地上下文。
        # 尺寸跟着**源图比例**走、跟渠道横竖无关（见 nai.prepare_image）——
        # 垫一张竖图进来不该被裁成横的。
        nai_i2i = None
        if str(source_image or "").strip():
            try:
                raw, note = comfy_src.resolve(source_image)
                image_b64, span_w, span_h = nai.prepare_image(raw)
                nai_i2i = {"image": image_b64,
                           "strength": _nai_strength(denoise), "note": note,
                           "width": span_w, "height": span_h}
            except RuntimeError as e:
                return str(e)
        intent = _intent_key(prompt, skill, lora)
        if image_jobs.find_pending_duplicate(target, target_id, intent) is not None:
            log.info("拦下重复生图（NAI）：%s %s 已有一张同参数的图在途",
                     target, target_id)
            return _DUPLICATE_NOTE
        # 二次确认闸（2026-10-04）：QQ 轮拦下发确认卡，对方回「好」才真入队。
        from app import confirm_gate
        gate = confirm_gate.intercept("nai", skill=skill, prompt=prompt,
                                      intent=intent, nai_i2i=nai_i2i,
                                      skip_confirm=_skip_confirm)
        if gate is not None:
            return gate
        return _enqueue_nai(prompt, target, target_id, nai_i2i, intent, skill)

    # 垫图（图生图）= **模型传没传 `source_image`**，代码不判原话（2026-10-06
    # 拆掉了这里的 `_i2i_gate`，见文件开头的留档）。什么时候该传写在下面两处文案：
    # 本文件末尾的工具描述与 `source_image` 参数说明、`agent_prompt._TOOL_HINTS`。
    #
    # 口径（用户拍板）：引用一张图本身**不是**垫图要求，默认只「看这张图 →
    # 反推提示词 → 画一张新的」；要垫图得他明说「qwen 图生图 + 改什么」。

    # 没点名 skill 时的默认渠道：文生图照旧 T2I_DEFAULT_SKILL。execute_tool 是 fn(**args)，
    # 模型不传 skill 就落到这里的默认值 None——所以「没点名」和「点名了默认渠道」
    # 分得开。
    if not skill:
        skill = T2I_DEFAULT_SKILL

    # 停用渠道的硬闸（见 config.DISABLED_IMAGE_SKILLS）。
    #
    # 放在这里而不是靠「不进白名单」：白名单只管**提示词里列不列**，模型要是
    # 记得这个名字，照样能把 skill 传进来。而 qwen 的代价不是「画得慢」，是
    # **把整机拖崩**——这种事必须有一道代码级的闸，不能指望模型自觉。
    #
    # 文案讲究：不把渠道名当技术名词甩给对方（群里看到 qwen_image_v1 很奇怪）；
    # 同时给出去路，别让模型以为「生图坏了」。
    if skill in DISABLED_IMAGE_SKILLS:
        return ("错误：" + skill + " 这个渠道已经停用（这台机器带不动它）。"
                "**别跟对方提这个渠道名，也别解释原因**——对方只是要一张图的话，"
                "直接改用默认的 " + T2I_DEFAULT_SKILL + " 重画"
                "（prompt 改写成动漫的标签式英文写法）；"
                "对方点名要它，就照实说这个渠道现在用不了。")

    # 种子在**动手之前**定下来（停用渠道那道闸之后：那种请求本来就不会画，
    # 先报渠道的问题更有用）。垫图要上传源图、skill 要读文件，一个抄错的种子
    # 不该先把这些做完再报错。
    seed, seed_pinned, seed_err = _resolve_seed(seed)
    if seed_err:
        return seed_err

    # 尺寸档守卫：模型见到「高清」就传 hd_*，但对方可能只是说画质形容词。
    # 放在停用渠道闸之后 —— 真被停用时报渠道的问题更有用。
    skill, hd_note = _hd_tier_guard(skill, T2I_DEFAULT_SKILL)

    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    # 图生图：给了源图就换成图生图工作流，并先把源图送进 ComfyUI 的 input
    # 目录。取图 / 缩放 / 上传任何一步失败都当场返回，**不退回文生图**——
    # 对方以为改的是自己那张，收到的却是凭空画的，比直接报错糟得多。
    workflow = skill_data["workflow"]
    # 降级 / 改判说明都走 source_note 这一个通道进回执。t2i_note 在
    # is_i2i 被清成 False 后走到这里，所以也要带上。
    source_note, denoise_txt, uploaded = hd_note + t2i_note, "", ""
    if is_i2i:
        if skill not in _I2I_SKILLS:
            return ("错误: " + skill + " 不支持图生图（垫图 / 改图）。"
                    "去掉 source_image 按文生图重来；对方确实要在这张图上改，"
                    "就改用 qwen_image_v1 + source_image（只改那一处、其余原样），"
                    "**不要换个渠道硬垫**。")
        # 重绘档吃这个数；qwen 的骨架里 denoise 写死 1、没有 `__DENOISE__`
        # 可替（见模块开头②），所以这里对它是个空操作。
        denoise_txt = "%.2f" % I2I_DENOISE
        i2i = load_workflow(
            os.path.join(skill_data["path"], "workflow_i2i.json"))
        if not i2i:
            return "错误: Skill '" + skill + "' 没有图生图工作流"
        try:
            raw, src_note = comfy_src.resolve(source_image)
            # 降级提示（`hd_note`）不能被垫图说明顶掉，两条都要传给回执
            source_note = (src_note + hd_note) if hd_note else src_note
            # 缩到本档画布的长边（不是 comfy_src 默认的 1216）：图生图的出图
            # 尺寸就是「这一步缩出来的尺寸 × 二段放大倍率」，所以高清档必须
            # 按自己的画布缩，否则垫图出来的还是源图那个大小。
            # qwen 那一档不受这个尺寸影响：它的文生图骨架是 EmptySD3LatentImage，
            # `_canvas_long_side` 读不出画布 → 落回 MAX_SIDE，而出图尺寸最终由
            # 骨架里 `resolution` 那个面积档决定——这一步对它只是归一化
            # （EXIF 方向 / 透明通道 / 8 的倍数），不是定尺寸。
            fitted, _size = comfy_src.fit(
                raw, max_side=_canvas_long_side(skill_data["workflow"])
                or comfy_src.MAX_SIDE)
            uploaded = comfy_src.upload(fitted)
        except RuntimeError as e:
            return str(e)
        workflow = i2i

    workflow_str = json.dumps(workflow)
    workflow = apply_prompt_placeholders(workflow_str, prompt, seed)
    if is_i2i:
        # 垫图专用占位符：源图文件名（按 JSON 字符串转义填，避免文件名里的
        # 引号把 JSON 打破）与重绘强度。这两个只在 workflow_i2i.json 里出现。
        workflow = _apply_i2i_placeholders(workflow, uploaded, denoise_txt)

    # 用户点名换 lora 才走这段；不传 lora 时一行替换逻辑都不执行，
    # 工作流原样提交，跟从前完全一样。
    if lora:
        err = _apply_loras(workflow, lora)
        if err:
            return err

    # 入队前先确认 ComfyUI 真的在。队列在 agent 侧，enqueue 从来不碰
    # ComfyUI，所以它挂了也照样「成功」，模型就会拿到一句「已经排上队了」
    # 去跟对方承诺，几十秒后 worker 才撞上连接失败——群里先看到承诺、再看
    # 到「图没画出来」，前后打架。探不到就当场拒掉，让模型老老实实说画不了。
    if not image_jobs.comfy_alive():
        return ("错误：ComfyUI 现在没在线（" + COMFYUI_URL + " 连不上），"
                "这张画不了。直接告诉对方现在画不了、让他稍后再试，"
                "不要说图已经在画了或者马上就好。")

    # 排进**全局串行队列**：同一时刻 ComfyUI 里最多只有一张图在跑，其余老老
    # 实实排队（见 image_jobs）。从前是这里直接 _queue_prompt 提交、排队发生
    # 在 ComfyUI 内部——agent 侧看不见也管不着，多个会话并发时 N×2 张一起灌
    # 进去，显存瞬间见底。会话身份在这一刻快照下来：worker 线程读不到 qq_api
    # 的线程本地上下文。
    from app import qq_api
    target, target_id = qq_api.current_context()
    # 同一件事还没出图又被提交一遍：不再排第二张。模型这一轮要么是没看到
    # 上一轮的回执（上下文被压缩 / 守卫误判成空头承诺），要么是把「再跑一张」
    # 当成了默认动作——两种都不该真的多出一张图。只在**在途**时拦（见
    # image_jobs.find_pending_duplicate）。
    intent = _intent_key(prompt, skill, lora, seed if seed_pinned else None)
    if image_jobs.find_pending_duplicate(target, target_id, intent) is not None:
        log.info("拦下重复生图：%s %s 已有一张同参数的图在途，不再排第二张",
                 target, target_id)
        return _DUPLICATE_NOTE
    # 二次确认闸（2026-10-04）：QQ 轮拦下发确认卡（渠道/种子/提示词全文），
    # 对方回「好」由 confirm_gate 原样入队；网页端放行。放在所有守卫之后、
    # 入队之前——存下的就是最终参数，确认后不需要重算任何东西。
    from app import confirm_gate
    gate = confirm_gate.intercept("comfy", skill=skill, prompt=prompt, seed=seed,
                                  intent=intent, workflow=workflow,
                                  note=source_note,
                                  skip_confirm=_skip_confirm)
    if gate is not None:
        return gate
    # prompt 一路带到队列里，只为出图后记账本（编号 → 提示词）；出图用的是
    # 上面填好的 workflow。seed 同样一路带到底：发图那行 caption 要贴它、
    # 账本要存它（见 image_jobs._caption / image_log.save）。
    job, reason = image_jobs.enqueue(target, target_id, workflow, skill,
                                     prompt=prompt, intent=intent, seed=seed,
                                     resample_fn=resample_fn)
    if reason is not None:
        # 拒收时工作流还在手上，ComfyUI 一点算力都没浪费，也不会留下「画了
        # 却没人发」的孤儿图。
        return reason
    # 接单成功才扣私聊额度（拒收一张不扣）。失败由 worker 的 _finish 退回来。
    quota_tail = _charge_quota(job, target, target_id)

    if target is not None:
        # 提交完立刻返回，图由 worker 画好后自己发回原群。留在这儿同步等会把
        # 适配层的并发槽（默认 2 个）占住几分钟——文本回复和别的群都得陪着等
        # 显卡。回执由工具**直接发**（见 _qq_receipt），不由模型转述。
        return _qq_receipt(job, target, target_id,
                           source_note=source_note, quota_tail=quota_tail)

    try:
        # 网页端：等到「排队 + 出图」全程。任务被超时中断时 image_jobs 会把
        # TimeoutError 挂到 job.error 上，这里接住当普通工具失败转述。
        history_entry = job.wait()
    except Cancelled:
        # 不把 Cancelled 抛给 execute_tool：那会被描述成"工具执行失败"，
        # 让模型以为工具坏了。中断是一个正常结局，说清楚就行。
        return "已中断：用户取消了等待。图片可能仍在后台生成，可到 ComfyUI 界面查看。"
    except TimeoutError as e:
        return "错误: " + str(e) + "，这张已经中断，换个提示词或稍后再试。"
    except Exception as e:
        # 其余失败（提交不上去、跑完了没图、ComfyUI 崩了）照样只回一句错话：
        # 直接冒到 execute_tool 会被描述成「工具坏了」，模型就该开始编了。
        return "错误: " + str(e)
    images = image_jobs.output_images(history_entry)

    if not images:
        return "错误: 生成完成但未找到输出图片"

    # 用相对路径（不带 host）：任何端(手机/平板/PC)访问时都用当前站点 origin 加载
    urls = ["/api/image/" + img for img in images]

    return ("生成成功！seed: " + str(seed)
            + ("（%s）" % source_note if source_note else "")
            + "\n图片地址:\n" + "\n".join(urls))


def _nai_strength(denoise):
    """denoise 参数 → NAI 的 strength（重绘噪声）。不传/写错用默认 0.7，
    越界钳回 [0.1, 0.9]——垫图不会「完全不变」也不会「完全看不出原图」。"""
    try:
        s = float(denoise)
    except (TypeError, ValueError):
        return nai_mod.NAI_I2I_STRENGTH
    return min(0.9, max(0.1, s))


def _enqueue_nai(prompt, target, target_id, nai_i2i=None, intent=None,
                 skill="nai"):
    """把一张 NAI 图排进全局串行队列（复用现有队列，见 image_jobs）。

    NAI 是云端调用，也占「这一轮」的并发，跟 ComfyUI 的图混在同一条队列里
    排队不会更慢，还能让对方看到「前面还有几张」。enqueue 的 workflow 字段
    在这里塞的是 prompt 字符串——cloud 分支靠 skill 判断怎么用它；
    nai_i2i 非 None 时是图生图（快照好的源图 base64 + 强度 + 出图尺寸）。

    `skill` 是**真实渠道名**（`nai` 竖版 / `nai_wide` 横版）：worker 靠它决定
    文生图的横竖，所以这里不能写死成 "nai"。
    """
    # 不传 prompt：NAI 走 _process_nai，图的 caption 不带编号、也不进账本
    # （用户选的「只做 anime」）。这里传了也是死数据。
    job, reason = image_jobs.enqueue(target, target_id, prompt, skill=skill,
                                     nai_i2i=nai_i2i, intent=intent)
    if reason is not None:
        # 拒收时什么算力都没花，也没有孤儿图。
        return reason
    # NAI 也占私聊额度（用户 2026-09-30 选的「一起算」）：额度是「私聊每天
    # 最多几张图」这个承诺，跟图是从本机还是云端出来的无关。
    quota_tail = _charge_quota(job, target, target_id)
    # 回执同样由工具**直接发**（见 _qq_receipt）：NAI 也占同一条队列，
    # 「前面还有几张」一样是后台现算的，没理由让模型去转述。
    return _qq_receipt(job, target, target_id,
                       source_note=(nai_i2i["note"] if nai_i2i else ""),
                       quota_tail=quota_tail)


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片。\n\n"
                  "【默认】不传 skill 就是 " + T2I_DEFAULT_SKILL + "（用户自定义的"
                  "Anima 渠道，底模和 6 个 LoRA 已烘焙在工作流里），一次一张，1024×1536。"
                  "prompt 写逗号分隔的标签式英文、只写一段，**不要用 --- 分隔**。\n\n"
                  "【动漫渠道只有这 16 个 = 4 画风 × 4 尺寸档，别编别的 skill 名出来】\n"
                  "- 画风（**渠道名就是画风**，按想要什么画风挑，不是按模型挑）："
                  "anima_clear（清透素净）/ anima_soft（柔光）/ "
                  "anima_gloss（冷调油光，用户直接叫它「anime2 / 原版」）/ anima_curvy（丰腴强光影）\n"
                  "- 尺寸档（**画风当后缀**，跟画风正交）：`anima_<画风>` 728~768×1024 默认不放大 / "
                  "`hd_fast_<画风>` 1024×1536 不放大、速度跟常规一样 / `hd_2_<画风>` 1328×2000 / "
                  "`hd_3_<画风>` 1536×2304 最大最慢最吃显存\n"
                  "**换渠道的门槛：只有用户点名画风 / 点名尺寸、或明确要「更柔 / 更亮 / 更丰满 / 更大」时才传，平时一律不传。**"
                  "clear 和 soft 像，分不清也走默认\n"
                  "**⚠️ 换不换尺寸档，只看一件事：他话里有没有「档位名或具体像素」。**\n"
                  "  ① **报了档位或像素**（快档 / 二档 / 三档 / `高清快档` / `高清二档` / `高清三档`，"
                  "或直接报「1328×2000」这类数字，或明说「当壁纸 / 要最大 / 竖屏长图」）"
                  "→ **换 hd_ 渠道**，对号入座："
                  "快档→`hd_fast_<画风>`、二档→`hd_2_<画风>`、三档→`hd_3_<画风>`；没说画风就用 clear 系。"
                  "他连档都报出来了，要的就是那张更大的图，别当没听见\n"
                  "  ② **只说了画质形容词**（高清 / 清晰 / 清楚 / 画质好 / 精细 / 不糊 / 大图，一个档位名都没有）"
                  "→ **不传 skill**，照默认渠道出图。这种词说的是「别糊」，不是「要更大尺寸」；"
                  "为它换成 hd_ 只是白等 30 秒、白吃显存，画质并不会更好\n"
                  "  判据就一句：**带数字或带「档」字 = 换档；只是形容词 = 别动。**\n"
                  "【三个备选渠道都是「点名才用」，别主动推荐、别当默认】\n"
                  "- **qwen_image_v1**（通义，1024×1536，**慢：一张 40 秒~1 分钟**）：要**画面里写出文字（尤其中文）**、"
                  "要**写实照片感**（真人摄影 / 商品图 / 场景照）、或提示词是**一长段自然语言描述**时才用。"
                  "它的 prompt 写**完整主谓宾的自然语言句子**，不写标签堆、不写负面词、不传 lora；只出单张，要多个变体分多次调用。"
                  "它也是**图生图唯一的那条路**（见下面【图生图只有一条路：qwen】）\n"
                  "- **krea2**（米山舞 retroanime，832×1216 直出）：只在用户点名 krea2 / 米山舞时传。"
                  "标签式英文，风格前缀工作流自动拼，**不要自己再写一遍**\n"
                  "- **nffa**（Illustrious 系，1024×1536，**慢：一张 40~75 秒**）：只在用户点名 nffa 时传。"
                  "标签式英文，**完整角色描述全自己写**——它不拼画风前缀、负面词也写死在工作流里，别再叠负面词。"
                  "也是 2 个 lora 槽（画风 + 描边），对它传 lora 会把画风顶掉。不支持垫图，不认 ` --- `\n"
                  "- **image_gen_v1**（SD / SDXL，832×1216）：**唯一支持一次出多张**的渠道——"
                  "prompt 里用 ` --- ` 分隔几段就出几张（其它渠道会把 --- 当普通文字）。"
                  "只在用户点名「用 sd / sd 模型」或要多个变体时用。不支持垫图\n\n"
                  "【silver = 默认渠道；silver-hd / jank 点名才用】`silver` 就是不传 skill 时走的那条"
                  "（Anima 底模 + 6 个 LoRA 已烘焙在工作流里，1024×1536），用户明说"
                  "「用 silver 画」时也可以显式传 `skill=silver`，效果一样。"
                  "`jank`（NoobAI 底模 + 4 个 LoRA，1024×1536，二段 2x 放大）**只在用户"
                  "明说 jank 时传**。`silver-hd`（同 silver 工作流，只把末尾放大换成 4x，"
                  "出 4928×7360、一张约 28MB）**只在用户明说「silver-hd」或「silver 超清」时传**。"
                  "三条都是**文生图专用、没有图生图骨架**；"
                  "prompt 照常写标签式英文、不传 lora 就用工作流里那套现成的；"
                  "对方要垫图/改图别选它们（图生图只走 qwen_image_v1）。\n"
                  "⚠️ `silver` 还常见作**发色词**（silver hair / 银发）——用户说「银发」"
                  "是在描述画面，不是点名渠道，别因为句子里有 silver 就传 skill；"
                  "但**默认本来就走 silver**，「银发的 X」直接写进 prompt、不传 skill 就行。\n\n"
                  "【角色：认得出就把名字写在 prompt 最前面】这是**还原度最高的一行**，"
                  "比一长串外貌描述管用得多。实测同一轮需求：写了 "
                  "`rudeus greyrat, mushoku tensei` 的画得像，只写「一位年轻男子」的那版角色全走形。\n"
                  "- 标签式英文渠道（anima_* / hd_* / image_gen_v1 / krea2 / nffa / nai）名字写"
                  "英文 tag 放最前：`rudeus greyrat, mushoku tensei`，冷门角色带作品名 "
                  "`hu tao \\(genshin impact\\)`\n"
                  "- **qwen_image_v1 的自然语言句子里照样要写名字**（「鲁迪乌斯（无职转生）」/ "
                  "`Rudeus Greyrat from Mushoku Tensei`）——「写自然语言」不等于不用专有名词，"
                  "只写「一位蓝发少女」出来就是路人脸\n"
                  "- 外貌**只补与原设定不同的地方**（换装 / 换发色 / 改年龄 / 特定姿势表情）；"
                  "原作本来就有、用户也没让改的（发型 / 瞳色 / 体型）名字已经带了，别重复堆，堆多了和原作设定打架\n"
                  "- **用户自己报了名字**（「画神里绫华」）→ 原样写进去，一个字都不许省\n"
                  "- **认不出来的角色**：照外貌描述画，但回复里要说明「这个角色我没认出来，是按外形画的」，别装认得。"
                  "用户看不对会告诉你名字，你再重画\n"
                  "- **所有渠道都没有角色底模**，别指望任何渠道自带角色。例外：qwen 改图那次只写改动指令，"
                  "角色由源图带着，不用重复写名字\n\n"
                  "【lora】用户点名要换 lora 时才传，平时不要传。格式「文件名:强度」逗号分隔"
                  "（如 \"x.safetensors:0.8,y.safetensors:0.5\"），文件名要完整（.safetensors 结尾），"
                  "写错会返回可用清单。传了就完全接管本次 lora，每个渠道 2 个槽，没填满的自动关闭——"
                  "**nffa 也一样是 2 个槽**（画风 + 描边），对它传 lora 会把画风顶掉、画的就不是那个味了。\n\n"
                  "【引用图片：默认只看，不改】用户引用一张图，**默认只是让你看得见它**：照它反推提示词、"
                  "用默认渠道画一张**全新的**（「看特征 / 复刻 / 参考这个风格 / 照着画一张新的 / 这图什么来头」"
                  "全是这条路），或者只是让你看图点评时直接回话。这些**都不传 source_image**——"
                  "光是引用了图，永远不构成图生图。\n"
                  " 引用的是**机器人自己画的图**（尾巴带 HT 编号 / 渠道 / 种子那一行）+ 用户提新需求 → "
                  "走账本拿它当初的提示词，看图、把需求并进提示词，**用那张图当初的渠道**重新生成"
                  "（他没点名换渠道就别换）。\n\n"
                  "【source_image 的门槛：只有明说「qwen 图生图」才传】默认**一律不传**。"
                  "只有对方**明说要在这张图上改**（说出机制名，一般还会点名 qwen）时才传 1。\n"
                  "只说「把衣服换成jk」「换个姿势」这类**改动内容**、没说要动这张图的，按"
                  "**改提示词重新画一张**处理（t2i，不垫图）。\n"
                  "**分不清是要改这张还是画一张新的就问一句**，别自己猜。\n\n"
                  "【图生图只有一条路：qwen_image_v1】`source_image` 填 1 = 垫**本轮出现的那张图**"
                  "（优先取对方引用的；他没引用就取他自己刚发的；两样都没有就垫不了，让他把图发出来再 @ 你一次）。\n"
                  "- 传法：`skill=qwen_image_v1` + `source_image=1`，prompt 只写**一句改动指令**"
                  "（祈使句：改哪里→改成什么，末尾补 keep everything else exactly the same），"
                  "**不要把整张图重新描述一遍**（那等于给模型一堆「这里也可以改」的许可）。"
                  "一次只交代一处改动最稳，要改三件事就分三次调用。\n"
                  "- **慢：一张 1~2 分钟**（动漫档 20~30 秒），所以它是点名才走的渠道，不是改图的默认做法"
                  "（2026-10-02 用户明确：「不要让 AI 默认就用 qwen，太耗时」）。\n"
                  "- **动漫档（`anima_*` / `hd_*`）的重绘已经从用法里撤掉**（2026-10-06 用户拍板："
                  "「Anima 的图生图没有 Qwen 好用，图生图只需要一个 Qwen 的途径」）。工作流骨架还在，"
                  "但**别拿它当图生图渠道**，也别跟对方提这条路。\n"
                  "取不到源图会当场报错——"
                  "**绝不退回文生图凭空画一张**，对方以为改的是自己那张，收到别的构图比直接说改不了糟得多。\n\n"
                  "【nai / nai_wide（NovelAI 云端）】**仅限管理员为特定群开通后**才能用，图由群主的 NovelAI 账号在云端出，"
                  "跟本机 ComfyUI 无关；本群没开通就传了会被直接拒绝——照实说这个渠道本群用不了、让对方去找群主开。\n"
                  "- 文生图：skill 传 nai（**竖版 832×1216**）或 nai_wide（**横版 1216×832**），"
                  "**只传 prompt，其它参数都不要传**。两张图成本一样，区别只有构图方向——"
                  "对方要的是**横向画面**时才传 nai_wide（风景 / 全景 / 房间 / 横躺、「宽的」「横的」「壁纸」「横幅」「封面」），"
                  "没提方向的一律走 nai。**「画布是竖的但内容可以横躺」不算**——构图方向看整张图的形状，不是你脑子里那个姿势。\n"
                  "- 图生图：skill 传 nai（或 nai_wide）+ **source_image 传 1**（门槛同上），"
                  "可选 denoise（0.1~0.9，默认 0.7，越大改得越狠，**别主动传**）。\n"
                  "- **垫图时出图尺寸跟着源图比例走**，传 nai 还是 nai_wide 都一样（垫一张竖图，出来还是竖的）——"
                  "所以对方说「把这张竖图改成横的」时别应承，直接说改不了；想换构图只能重新写提示词画一张新的。",
    # 2026-09-30：`description_overrides` / `hidden_params` 都删了。
    # 它们本来只为「QQ 侧藏掉 use_character 和角色底模那套」而存在；
    # 角色底模随 SD 渠道一起下线后，两个 agent 看到的描述已经没差别，
    # 留一份 QQ 专用文案只会多一处要同步维护的副本。
    # `agent_prompt.py` 取不到 `description_overrides` 时会自动回落到
    # `tool["description"]`，所以删掉是安全的。
    "function": _generate_image,
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "提示词。**开头先写角色名**（认得出就写：英文 tag 渠道 `rudeus greyrat, mushoku tensei`；qwen 的自然语言句子里写「鲁迪乌斯（无职转生）」）——没有任何渠道自带角色，**名字才是还原度最高的那一行**，外貌只补与原设定不同的部分。\n默认渠道写**逗号分隔的标签式英文短句，只写一段、不要用 --- 分隔**（只有 skill=image_gen_v1 认 ` --- ` 分隔、一次出多张；其它渠道会把 --- 当普通文字，要出多张就分多次调用、每次一个变体）。\n两个例外：skill=qwen_image_v1 写**自然语言句子**（不写标签堆）；**它当改图渠道用时（同时传 source_image）只写一句改动指令**，例如 `change her coat to red, keep the pose, face and background exactly the same`——**不要把整张图重新描述一遍**。"},
            "skill": {"type": "string", "description": "渠道名。**不传就是默认渠道 " + T2I_DEFAULT_SKILL + "**（用户自定义的 Anima 渠道）。可选值见 Available Skills 的生图类（16 个动漫渠道 = 4 画风 × 4 尺寸档，另有 silver / silver-hd / jank / qwen_image_v1 / image_gen_v1 / krea2 / nffa / nai / nai_wide），各自画风/尺寸/场景/速度见那一行里。\n**只在用户点名画风 / 尺寸 / 渠道时才传，平时一律不传**。**「高清快档 / 高清二档 / 高清三档」算点名尺寸，要传对应 hd_fast_ / hd_2_ / hd_3_**；但光说「高清 / 清晰 / 画质好」不算，那只是形容词，照默认不传。\n分不清就照 Available Skills 那行摘要选，选错用户会说；细节可 load_skill 读该渠道主规范。"},
            "lora": {"type": "string", "description": "可选。「文件名:强度」逗号分隔，如 x.safetensors:0.8,y.safetensors:0.5。仅在用户点名要换 lora 时传，每个渠道 2 个槽"},
            "source_image": {"type": "string", "description": "垫图 / 图生图：填 1 = 垫本轮出现的那张图（优先取对方引用的，其次他自己刚发的；两样都没有会报错）。\n**默认不传**。只有对方**明说要在这张图上改**（说出「图生图 / 垫图」这类机制名，通常还会点名 qwen）时才传。只说「把衣服换成jk」这类**改动内容**不算——那是照这张图**改提示词重新画一张新的**（不垫图）。引用一张图本身**永远不是**垫图要求：看看 / 点评 / 反推 / 照它画新的，都不传。\n传了就**必须配 `skill=qwen_image_v1`**（参考图编辑，只改那一句交代的地方、其余原样；慢，一张 1~2 分钟），prompt 只写**一句改动指令**。\nskill=nai 也支持垫图（云端）。动漫档（anima_* / hd_*）的重绘骨架还在，但**用法上已撤掉**，别再拿它当图生图渠道。本机渠道的重绘强度是定死的，传 denoise 也没用。"},
            "seed": {"type": "integer", "description": "生图种子，**只在对方点名要「用某个种子重画 / 换提示词再来一张」时才传**，平时一律不传（不传=随机）。范围 0 ~ 4294967295 的整数，填错格式/超界会直接报错，别猜。种子会跟着编号印在图那行 caption 上（`编号 · 分辨率 · 渠道 · seed 数字`），对方引用那条消息时能一起带回来。⚠️ 同一个种子只有配**同样的提示词 + 同样的渠道 + 同样的 lora**才画得出同一张图（改提示词重画=构图大体在、细节变）；动漫渠道是两段采样、两段共用这一个种子，所以只有这一个数。**只有本机渠道认**（anima_* / hd_* / qwen_image_v1 / image_gen_v1 / krea2 / nffa），skill=nai 传了会被拒；image_gen_v1 一次出多张时第 k 张 = 这个数 + k - 1"}
        },
        "required": ["prompt"]
    }
}
