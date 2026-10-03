"""ComfyUI 生图工具"""

import os
import logging
import re

import requests
import json
import random
from flask import request
from app import comfy_src, image_jobs
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
# 对方**要图生图**才传 `source_image`（用户原话：「除非用户说到要图生图，不然
# 就只是反推提示词生成」）。2026-10-04 起判据放宽：他**自己发了图**就算，不必
# 再说一遍「图生图」——把图递过来就是指名改它。这条边界写在**三处**：
#   1. **硬闸** `_i2i_gate`（本文件下方 + 函数开头那道）——他既没自己发图、
#      打的字里也没有「要动这张图」的意思，直接拒，一个字节都不往 ComfyUI 送。
#      判据用原话不用模型的判断，因为模型一看见引用图就想改（见那道闸的注释）；
#   2. 本文件末尾的工具描述与 `source_image` / `prompt` 参数说明；
#   3. `agent_prompt._TOOL_HINTS` 的 generate_image 条目。
# 硬闸是 2026-10-02 加的：只靠 2、3 两道文案时模型照样自作主张（2026-09-27 那次
# 整条链路停用是同一个毛病），而 qwen 一旦误触发就是一分多钟的白等 + 占着队列。
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

# ─── 图生图的触发闸门（2026-10-02 用户拍板）─────────────
#
# 用户的原话：「除非用户说到要图生图，不然就只是反推提示词生成；图生图没有明确
# 说明，AI 就不要使用。」——**这条不是文案能兜住的**，写在提示词里模型照样会
# 自作主张（2026-09-27 那次整条链路停用就是同一个毛病），所以做成硬闸：
# `source_image` 传了，但本轮**对方自己打字的那段话**里找不到任何「要动这张图」
# 的意思，就当场拒，不往 ComfyUI 送一个字节。
#
# 为什么判据是「对方原话」而不是「模型怎么想」：模型在群里看不见图片地址，
# 只能靠 `source_image=1` 指代「本轮引用的那张」，它一看见引用图就有极强烈的
# 冲动去改它（qwen 那条尤其贵，一张 1~2 分钟）。原话是唯一没被模型加工过的证据。
#
# 词表只收**两类**：点名机制的词（图生图 / 垫图 / 改图…），和明确要动这张图的
# 动词（换成 / 去掉 / 重画…）。
#
# 故意**不收**「这张 / 这图 / 原图 / 参考这张」这类指示代词：对方引用一张图时
# 十句里有八句带「这张」（「这张好看吗」「参考这张画个新的」），收进来等于给
# 闸门开后门——判据就没了。而拦错的代价也不是死路：拒绝话术本来就指引模型
# 按反推提示词画一张新的，那正是用户要的默认行为。
# （误放的代价是白花 1~2 分钟画一张没人要的图，两边不对称，所以往紧里收。）
_I2I_ASK_WORDS = (
    # ① 点名机制的
    # 「垫」单字也收：这个词在生图群里基本只出现在「垫图」的意思里
    # （「垫这张图」「用这张垫一下」中间能插字，逐条列不如认这个字）。
    "图生图", "垫", "垫图", "垫个图", "垫张图", "垫一张图", "改图",
    "改一下图", "修图", "p图", "重绘", "再绘", "i2i", "img2img",
    "image to image", "改这张", "动这张", "这张改",
    # ② 要动这张图的动词（多半连着「把 X 换成 Y」说）
    "换成", "换掉", "换个", "换件", "换身", "换一套", "换背景", "替换",
    "改成", "改为", "调成", "变成", "去掉", "删掉", "删除", "擦掉", "抹掉",
    "移除", "加上", "加个", "添上", "补上", "换装", "换衣",
    "重画", "重新画", "再画", "接着画", "继续画",
    "改一下", "改改", "修改", "微调", "别动其余", "其余别动",
    "上色", "改色", "变装",
    # 英文原话（少见，但对方打英文时别拦住）
    "change", "remove", "replace", "edit", "redraw",
)

# 换 / 改 这两个词根后面能挂的量词和补语太多（「换一件」「改一张」「换套衣服」
# 「改为红色」），列不完，所以对这两个词根单独配一条小正则。量词表里刻意
# **不含「了」「我」「批」**：「她改了什么」「换我做头像」是在聊别的，不是改图。
_I2I_EDIT_RE = re.compile(r"[换改][一二几]?[成掉为变个件张套身下]")

# 拒的时候给模型的话。三句各有用处，一句都别删：
#   1. 明确「不画」——不留「要不要试试」的余地，否则它会换个参数再调一次；
#   2. 给出路——把这一轮按它本来该有的样子做（看图 → 反推提示词 → 画张新的）；
#   3. 教它什么时候该开口问——对方话说得含糊时问一句，比闷头改图或闷头重画都好。
_I2I_NO_INTENT_NOTE = (
    "错误：对方这轮**没有说要图生图**，所以不垫图、不改图，本次调用没画任何东西。"
    "他只是**引用**了一张图、一个字没说——那默认是「给你看」，不是要改它。"
    "按引用那张**反推提示词**、当文生图重新画一张新的"
    "（去掉 source_image 再调一次就行）。"
    "如果对方的意思你真的拿不准（听起来像要改这张、又没明说），"
    "别猜，先回一句问：「是要改这张，还是照它画一张新的？」"
)


def _i2i_gate(is_i2i):
    """对方没明说要图生图 → 返回一句拒绝话术；该放行返回 ""。

    只在 QQ 会话轮里判（`current_turn_text()` 给的是 None 时**一律放行**：
    网页端和单元测试里没有「对方原话」这个证据源，不能把它当成「没说要改」）。

    **对方本轮自己发了图 = 放行**（2026-10-04 用户拍板）：把图递过来本身就是在
    指名「改这张」，还要他再说一遍「图生图」纯属重复确认——2026-10-04 01:5x
    他连着踩了三轮才等到图，就是这里只认原话造成的。只**引用**别人的图、又
    一个字没说要改的仍拦：那种十有八九只是「你看这张」。
    """
    if not is_i2i:
        return ""
    from app import qq_api

    if qq_api.current_own_images():
        return ""
    text = qq_api.current_turn_text()
    if text is None:
        return ""
    low = text.lower()
    if any(w in low for w in _I2I_ASK_WORDS) or _I2I_EDIT_RE.search(low):
        return ""
    log.info("拦下图生图：本轮原话里没有改图的意思（%r）", text[:60])
    return _I2I_NO_INTENT_NOTE


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
#   - `anima_clear`（**默认**）：清透素净、光最平。realskin → realskin（同一块底模）。728×1024。
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
# ⚠️ 默认渠道换过两次（`anima_soft` → `anima_clear`，2026-09-30 21:3x 用户拍板），
# **每次换都会连累一批写死名字的地方**（工具描述、拒收话术、4 个 skill.md 的「默认」标记、
# `comfy_workflow.py` 的默认参数、以及一批测试）。所以：
#   - 下面这个常量是**唯一真相源**，话术/描述里要提默认渠道名一律 `%` 它，别写字面量；
#   - 换默认渠道时按 `tests/test_image_channels.py::DefaultChannelTest` 的报错清单挨个改。
#
# 工作流都直接取自用户 ComfyUI 里导出的文件——他每调一次就要重导一次，
# 步数/CFG/采样器/放大倍率**都是他随手调的旋钮，别把这里的数字当契约**。
# 历史：更早有过「单底模 anima」「双底模 anima_2」「anima_realskin / anima 两个渠道」
# 等阶段，都已随本次切换归档到 `skills/_archive_20260930/`。
T2I_DEFAULT_SKILL = "anima_clear"


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


def _generate_image(prompt, skill=None, lora=None, source_image="",
                    denoise=None, seed=None, use_character=None):
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

    # 图生图的触发判据（对方没明说要图生图就别垫图，见 `_i2i_gate`）。
    # 放在 NAI 分流**之前**：那是同一个 `source_image` 参数，不该因为走的
    # 是云端就漏判。
    is_i2i = bool(str(source_image or "").strip())
    refused = _i2i_gate(is_i2i)
    if refused:
        return refused

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
        return _enqueue_nai(prompt, target, target_id, nai_i2i, intent, skill)

    # 垫图（图生图）：**模型传了 source_image 才算**，走下面那个 i2i 分支，
    # 而「能不能算」已经在上游过了一道硬闸（`_i2i_gate`，就在这个函数开头）。
    #
    # 引用一张图本身**不触发它**——默认仍然是「只看，照它反推提示词画一张新的」。
    # 这条边界写在两处：代码里的 `_i2i_gate`（本轮原话没有改图的意思就当场拒）
    # + 本文件末尾的工具描述（【引用图片】段）。别只留文案那道：2026-09-27 整条
    # 链路停用，就是因为模型「一看见引用图就往改图上想」。

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

    skill_data = load_skill(skill)
    if not skill_data or not skill_data["workflow"]:
        return "错误: 找不到 Skill '" + skill + "'"

    # 图生图：给了源图就换成图生图工作流，并先把源图送进 ComfyUI 的 input
    # 目录。取图 / 缩放 / 上传任何一步失败都当场返回，**不退回文生图**——
    # 对方以为改的是自己那张，收到的却是凭空画的，比直接报错糟得多。
    workflow = skill_data["workflow"]
    source_note, denoise_txt, uploaded = "", "", ""
    if is_i2i:
        if skill not in _I2I_SKILLS:
            return ("错误: " + skill + " 不支持图生图（垫图 / 改图）。"
                    "去掉 source_image 按文生图重来；对方确实要垫图，就换一个"
                    "支持的渠道——**只改画面里的一处、其余保持原样**用 "
                    "qwen_image_v1，**照那张的构图重画一张本渠道画风的**用"
                    "常规档 / 高清快档 / 二档。")
        # 重绘档吃这个数；qwen 的骨架里 denoise 写死 1、没有 `__DENOISE__`
        # 可替（见模块开头②），所以这里对它是个空操作。
        denoise_txt = "%.2f" % I2I_DENOISE
        i2i = load_workflow(
            os.path.join(skill_data["path"], "workflow_i2i.json"))
        if not i2i:
            return "错误: Skill '" + skill + "' 没有图生图工作流"
        try:
            raw, source_note = comfy_src.resolve(source_image)
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

    # 替换占位符。seed 已经在上面定好了（对方点名就照用，没点名才随机）。
    #
    # ⚠️ 动漫系渠道（anima_* / hd_*_*）是**两段采样**，工作流里有两个 KSampler、
    # 两处 `__SEED__`，而下面这两行是**全局替换** ⇒ 一二段拿到的是同一个数。
    # 所以「这张图的种子」始终就是报出去的那一个数：把它填回 ComfyUI 的两个
    # KSampler 就能复现。别看到「两个采样器」就以为要报两个种子。
    # __MULTI_PROMPTS__ 可以独占一个 JSON 字符串，也可以嵌在更大字符串里
    # （如节点 4 = "@kibro, __MULTI_PROMPTS__"，工作流自带固定画风/触发词前缀）。
    # 统一按「字符串内部转义替换」处理，两种都兼容。
    prompt_escaped = prompt.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '')
    workflow_str = workflow_str.replace("__MULTI_PROMPTS__", prompt_escaped)
    # 连引号一起换掉：ComfyUI 的 KSampler.seed 是 INT 字段，只替内容、留着引号
    # 就变成字符串 "123456"，提交时类型校验不过。老写法是给自定义节点用的
    # （它对 seed 类型不敏感），换成标准 KSampler 后必须落成真数字。
    workflow_str = workflow_str.replace('"__SEED__"', str(seed))
    workflow_str = workflow_str.replace("__SEED__", str(seed))  # 兜底：裸占位符
    if is_i2i:
        # 垫图专用占位符：源图文件名（按 JSON 字符串转义填，避免文件名里的
        # 引号把 JSON 打破）与重绘强度。这两个只在 workflow_i2i.json 里出现。
        workflow_str = workflow_str.replace('"__SOURCE_IMAGE__"',
                                            json.dumps(uploaded))
        workflow_str = workflow_str.replace('"__DENOISE__"', denoise_txt)

    workflow = json.loads(workflow_str)

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
    # prompt 一路带到队列里，只为出图后记账本（编号 → 提示词）；出图用的是
    # 上面填好的 workflow。seed 同样一路带到底：发图那行 caption 要贴它、
    # 账本要存它（见 image_jobs._caption / image_log.save）。
    job, reason = image_jobs.enqueue(target, target_id, workflow, skill,
                                     prompt=prompt, intent=intent, seed=seed)
    if reason is not None:
        # 拒收时工作流还在手上，ComfyUI 一点算力都没浪费，也不会留下「画了
        # 却没人发」的孤儿图。
        return reason
    # 接单成功才扣私聊额度（拒收一张不扣）。失败由 worker 的 _finish 退回来。
    quota_tail = _charge_quota(job, target, target_id)

    if target is not None:
        # 提交完立刻返回，图由 worker 画好后自己发回原群。留在这儿同步等会把
        # 适配层的并发槽（默认 2 个）占住几分钟——文本回复和别的群都得陪着等
        # 显卡。
        ahead = image_jobs.ahead_of(job)
        if ahead > 0:
            return ("已经排上队了（前面还有 %d 张），排到就画，"
                    "画好会自动发到群里。"
                    "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                    "直接把想说的话说完就行。" % ahead + quota_tail)
        return ("已经在画了，画好会自动发到群里。"
                + ("垫的是%s。" % source_note if source_note else "")
                + "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" + quota_tail)

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
            + ("（垫图：%s）" % source_note if source_note else "")
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
    ahead = image_jobs.ahead_of(job)
    if ahead > 0:
        return ("已经排上队了（前面还有 %d 张），排到就画，"
                "画好会自动发到群里。"
                "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" % ahead + quota_tail)
    if nai_i2i:
        return ("已经在画了（垫的是%s），画好会自动发到群里。"
                "不要输出图片地址，也不要说「图在下面 / 稍等」，"
                "直接把想说的话说完就行。" % nai_i2i["note"] + quota_tail)
    return ("已经在画了，画好会自动发到群里。"
            "不要输出图片地址，也不要说「图在下面 / 稍等」，"
            "直接把想说的话说完就行。" + quota_tail)


tool = {
    "name": "generate_image",
    "description": "调用 ComfyUI 生成图片。"
                  "【默认 Skill】文生图默认 anima_clear（Anima 2B 动漫模型，一次一张，"
                  "两段采样，实际出图 728×1024），不传 skill 就是它—— prompt 只写一段画面描述，**不要用 --- 分隔**。"
                  "【16 个动漫渠道 = 4 画风 × 4 档尺寸】**本机的动漫渠道只有这 16 个，别去编别的 skill 名出来**。"
                  "第一类是**画风**渠道（4 个，按**想要什么画风**挑，不是按模型挑），渠道名就是画风："
                  "anima_clear（默认）= 光最平最均匀、最素净（用户会说「清透 / 素净 / 自然 / 光别那么硬」）；"
                  "anima_soft = 柔光哑光、皮肤素净、对比低、层次稍多（「柔一点 / 干净 / 温柔」）；"
                  "anima_gloss = 冷调偏蓝、油光高光明显、锐利有 3D 渲染感、脸偏成熟"
                  "（用户会说「亮面 / 油光 / 高光 / 冷色 / 通透 / 清晰」，也会直接叫它「anime2 / 原版」）；"
                  "anima_curvy = 胸围明显更大、光影强烈、氛围浓"
                  "（用户会说「丰满 / 大胸 / 身材好 / 光影强 / 氛围感」）。"
                  "另一个维度是**尺寸档**（4 档，跟画风正交——每个画风都有 4 档尺寸）："
                  "普通档 `anima_<画风>` = 728~768×1024（默认，不放大）；"
                  "`hd_fast_<画风>` = 1024×1536、不放大，**速度跟常规渠道差不多**（要快就走它）；"
                  "`hd_2_<画风>` = 再 1.3× 放大到 1328×2000（中间档）；"
                  "`hd_3_<画风>` = 再 1.5× 放大到 1536×2304，**最大，但也最慢、最吃显存**"
                  "（只有用户明确说「最大 / 最高清 / 当壁纸」才用它）。"
                  "**用户只说「大图 / 高清」没说多大 → 往小的挑 `hd_fast_clear`；"
                  "说了尺寸但没说画风 → 用 clear**（跟默认画风一致）。"
                  "**只有用户明确点名画风 / 点名尺寸、或明确要「更柔 / 更亮 / 更丰满 / 更大」时才换渠道**；"
                  "平时一律不传 skill。anima_clear 和 anima_soft 很像，分不清时也走默认。"
                  "**用户只说「高清」但没提要更大 → 还是走默认的 anima_clear**，"
                  "别自作主张去用 hd_*（那三个又慢又占显存）。"
                  "【qwen_image_v1（通义）】**要画面里写出文字（尤其中文）**、要"
                  "**写实照片感（真人摄影 / 商品图 / 场景照）**、或提示词是**一长段自然语言描述**时，"
                  "传 skill=qwen_image_v1（**1024×1536 竖版**）。它的 prompt 要写**自然语言句子**"
                  "（完整主谓宾、像在跟人描述画面），**不要写标签堆、也不要写负面提示词**，"
                  "不传 lora；只出单张，要多个变体就分多次调用。"
                  "**慢渠道：一张约 40 秒，别的渠道 20~30 秒，所以它绝不是默认**。"
                  "图生图也一样——**默认走动漫重绘**，只有「只动那一处、其余一分不动」"
                  "或对方点名时才选它，见下面【图生图走哪条】。"
                  "【krea2】只在**用户明确点名 krea2**（或说「米山舞 / retroanime 那个工作流」）"
                  "时才传 skill=krea2；它是**备选，别主动推荐、别拿它当默认**。画风是 Yoneyama Mai"
                  "（米山舞），832×1216 单段直出（已去掉 2x 超分，不再出超大图）；"
                  "提示词按**标签式英文**写，风格前缀工作流会自动拼上、**不要自己再写一遍**。"
                  "【nffa】只在**用户明确点名 nffa**（或指着 nffa 画出来的那张要同款画风）时才传"
                  " skill=nffa；它跟 krea2 一样是**备选，别主动推荐、别当默认**。画风是 Illustrious 系"
                  "底模 `waiIllustriousSDXL_v150` + 画风 LoRA `NffaV1.3`（链尾再叠一层描边），"
                  "**1024×1536 竖版、一次一张**，"
                  "出图前固定跑两段修复（先修手、再修脸）。提示词写**标签式英文**、完整角色描述"
                  "**全自己写**——这个渠道**不拼任何画风前缀**（跟 krea2 不同），负面词也写死在工作流里、"
                  "**别再往 prompt 里叠一串负面词**。"
                  "**慢：一张 40~75 秒**，跟 hd 二档 / 三档一个量级，"
                  "所以更别拿它当默认。它**不支持垫图 / 改图**，也不认 ` --- ` 批量分隔。"
                  "【image_gen_v1（SD / SDXL）】只在**用户点名「用 sd / sd 模型」**，"
                  "或**要一次出多张变体**时传 skill=image_gen_v1（832×1216，SDXL 底模）。"
                  "它是**唯一支持一次出多张**的渠道：prompt 里用 ` --- ` 分隔几段就出几张"
                  "（其它渠道会把 --- 当成普通文字，只有它认）。"
                  "它**不支持垫图 / 改图**：要图生图就换动漫档（默认那条）或 "
                  "qwen_image_v1（慢，只在「只改一处」或点名时）。"
                  "【角色】所有本机渠道**都没有固定角色底模**，你在 prompt 中必须自己写出完整角色提示词"
                  "(发型/发色/瞳色/体型/服装/年龄等)，不要指望任何渠道自带角色。"
                  "**qwen 改图那一次不算**：那里只写改动指令，角色由源图带着。"
                  "【lora】用户点名要换 lora 时才传 lora 参数，平时不要传。格式「文件名:强度」，"
                  "多个逗号分隔（如 \"x.safetensors:0.8,y.safetensors:0.5\"）；文件名要完整"
                  "(.safetensors 结尾)，写错会返回可用清单；传了就完全接管本次的 lora，"
                  "每个渠道 2 个槽，没填满的槽自动关闭——**nffa 也一样是 2 个槽**"
                  "（画风 + 描边），对它传 lora 会把 nffa 的画风顶掉、画的就不是那个味了。"
                  "【引用图片：默认只看，不改】用户引用一张图，**默认只是让你看得见它**："
                  "照它反推提示词、用默认渠道画一张**全新的**（「看特征 / 复刻 / 参考这个风格 / "
                  "照着画一张新的 / 这图什么来头」全是这条路），或者对方只是让你看图点评时直接回话。"
                  "这些**都不传 source_image**。光是引用了图，永远不构成图生图。"
                  "【图生图的门槛：他自己发了图，或明说】满足任一条才传 `source_image`——"
                  "① 对方**这一轮自己发了一张图**（把图递过来就是指名改它）；"
                  "② 他打的话里明确要动这张图：说出「图生图 / 垫图 / 改图 / 重绘」这类词，"
                  "或者点名了要改的内容（「帮我把图片里这个角色换成 XXX」「去掉她手里那把伞」"
                  "「基于这张重新画一张」）。**两条都不满足就当没这回事**——系统另有一道闸，"
                  "那时你传了会被当场拒掉、一张都不画。"
                  "**只引用别人的图、自己一个字没说要改的，当「给你看」**："
                  "引用图的人十句八句带「这张」，指示代词不算意图。"
                  "听起来像要改、又没说明白，就回一句问：「是要改这张，还是照它画一张新的？」"
                  "【图生图走哪条：默认动漫重绘，选 qwen 得有理由】`source_image` 填 1 = "
                  "垫**本轮出现的那张图**（优先取对方引用的；他没引用就取他自己刚发的；"
                  "两样都没有就垫不了，让他把图发出来再 @ 你一次）。"
                  "① **重绘（默认走这条）**——走动漫 12 档（常规档 `anima_<画风>`、"
                  "`hd_fast_<画风>`、`hd_2_<画风>`），20~30 秒一张，prompt 照旧是**完整英文"
                  "标签串**（含角色描述）。**对方引用的就是机器人自己刚画的那张时（尾巴带"
                  "渠道名和种子那行）尤其走这条**——他说「基于这个重新画」要的是同渠道再来一张。"
                  "**`hd_3_<画风>` 不支持垫图**（最慢那档不给）。重绘强度是渠道定死的，不用你"
                  "操心；出图**尺寸跟着渠道走**（高清档就是高清），跟原图多大无关。"
                  "② **改图（qwen_image_v1，慢：一张 1~2 分钟，不是默认选项）**——"
                  "只有动漫重绘给不出来的结果才选它：**只动那一处、其余一分不动**"
                  "（换一件衣服的颜色、去掉一个物件、改画面里的文字），或者对方**点名**"
                  "qwen / 通义。选它之前先掂量那一分多钟值不值；对方只泛泛说了句「改一下」"
                  "而没说要保住其余部分，走①。传法：`skill=qwen_image_v1` + "
                  "`source_image=1`，prompt 写**一句改图指令**（祈使句：改哪里→改成什么，"
                  "末尾补 keep everything else exactly the same），**不要把整张图重新描述一遍**"
                  "（那等于给模型一堆「这里也可以改」的许可）。一次只交代一处改动最稳，"
                  "要改三件事就分三次调用。改图那条出图跟源图同比例、约 1024² 那一档，"
                  "不会自己变大图。"
                  "**分不清是要改还是要新的就问一句**，别自己猜。"
                  "取不到源图会当场报错——**绝不退回文生图凭空画一张**，"
                  "对方以为改的是自己那张，收到别的构图比直接说改不了糟得多。"
                  "【nai / nai_wide / NovelAI】**仅限管理员为特定群开通 NAI 后**才能用，"
                  "图由群主自己的 NovelAI 账号在云端出，跟本机 ComfyUI 无关；"
                  "本群没开通就传了会被直接拒绝，照实说这个渠道本群用不了、"
                  "让对方去找群主开。"
                  "文生图：skill 传 nai（**竖版 832×1216**）或 nai_wide（**横版 1216×832**），"
                  "**只传 prompt，其它参数都不要传**。两张图成本一样，区别只有构图方向——"
                  "对方要的是**横向的画面**时才传 nai_wide（风景 / 全景 / 房间 / 横躺、"
                  "「宽的」「横的」「壁纸」「横幅」「封面」这类说法），"
                  "没提方向的都走 nai。**「画布是竖的但内容可以横躺」不算**——构图方向看的是"
                  "整张图的形状，不是你脑子里那个人物的姿势。"
                  "图生图（改图 / 垫图）：skill 传 nai（或 nai_wide）+ **source_image 传 1**"
                  "（= 本轮出现的那张图：优先引用，其次他自己刚发的；两样都没有就画不了，"
                  "让他把图发出来再 @ 一次），可选 denoise（0.1~0.9，默认 0.7，"
                  "越大改得越狠，别主动传）——**只在对方明确要改图 / 垫图时才传 "
                  "source_image**，看图 / 点评照旧不传。"
                  "**垫图时出图尺寸跟着源图比例走**，传 nai 还是 nai_wide 都一样"
                  "（垫一张竖图，出来还是竖的）——所以对方说「把这张竖图改成横的」时"
                  "别应承，直接说改不了；想换构图只能重新写提示词画一张新的。",
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
            "prompt": {"type": "string", "description": "提示词。写逗号分隔的标签式英文短句，**默认只写一段、不要用 --- 分隔**（只有 skill=image_gen_v1 认 ` --- ` 分隔、一次出多张；其它渠道会把 --- 当普通文字，要出多张就分多次调用、每次一个变体）。**必须包含完整角色描述**（发型/发色/瞳色/体型/服装/年龄等）——没有任何渠道自带角色。两个例外：skill=qwen_image_v1 写**自然语言句子**（不写标签堆）；**它当改图渠道用时（同时传 source_image）只写一句改动指令**，例如 `change her coat to red, keep the pose, face and background exactly the same`——**不要把整张图重新描述一遍**"},
            "skill": {"type": "string", "description": "Skill名称。**不传就是默认 anima_clear**。可选值共 16 个（= 4 画风 × 4 档尺寸，见 Available Skills 的生图类）——4 个**画风**渠道（普通档 anima_<画风>，728~768×1024）：anima_clear（默认，清透最平光）/ anima_soft（柔光素肌，层次稍多）/ anima_gloss（冷调油光，用户也叫它 anime2）/ anima_curvy（丰腴强光影）；再叠 3 档**大图**（画风当后缀）：hd_fast_<画风>（1024×1536，不放大最快）/ hd_2_<画风>（1328×2000，1.3× 放大，中间档）/ hd_3_<画风>（1536×2304，1.5× 放大，最大最慢，最吃显存）。**只在用户点名画风 / 尺寸时才传**，平时不传。另有 qwen_image_v1（通义，**1024×1536 竖版**，要**画面写中文文字 / 写实照片感 / 长自然语言提示词**时走它；**图生图默认不走它**——动漫档重绘是默认那条，只有「只动那一处、其余一分不动」或者对方点名它时才 `source_image=1` + 一句改动指令，**它慢，一张 40 秒~1 分钟**）；image_gen_v1（**SD / SDXL**，832×1216，**唯一能一次出多张**的渠道：prompt 用 ` --- ` 分隔；不支持垫图。用户点名「用 sd」或要一次出多个变体才用它）；krea2（Krea2 Turbo + 米山舞画风，832×1216 直出，**只在用户点名时用**，别主动推荐）；nffa（Illustrious 系画风 + 手脸两段修复，画布 **1024×1536**、出图 1128×1688，标签式英文、不拼风格前缀、负面词已写死，**只在用户点名时用**，一张 40~75 秒，不支持垫图）；nai / nai_wide（NovelAI 云端，**仅限已开通的群**，文生图 / 图生图都走它：nai = 竖版 **832×1216**，nai_wide = 横版 **1216×832**，对方要横向构图时才用后者；垫图时出图尺寸跟着源图比例走、跟渠道横竖无关）"},
            "lora": {"type": "string", "description": "可选。「文件名:强度」逗号分隔，如 x.safetensors:0.8,y.safetensors:0.5。仅在用户点名要换 lora 时传，每个渠道 2 个槽"},
            "source_image": {"type": "string", "description": "垫图 / 图生图：填 1 = 垫本轮出现的那张图（优先取对方引用的，他没引用就用他自己刚发的那张；两样都没有会报错）。**默认不传**——**对方自己发了图、或者他明确要**图生图时才传：① 他自己发了图（把素材递过来了）；② 他说出「图生图 / 垫图 / 改图 / 重绘」这类词，或者点名要动什么（「把图里这个角色换成 XXX」「去掉那把伞」「基于这张重新画一张」）。**只引用别人的图而自己没说要改、或者只是看图 / 点评 / 照它反推提示词画张新的，任何渠道都不要传这个参数**；系统会查本轮的原话与本轮发图，两条都不满足就传了会被当场拒掉、一张都不画。传了之后**默认走动漫 12 档重绘**（anima_* / hd_fast_* / hd_2_*，20~30 秒，prompt 照旧写完整标签串；hd_3_* 不支持），**只有「只动那一处、其余一分不动」或者对方点名 qwen 才用 skill=qwen_image_v1**（慢，一张 1~2 分钟，prompt 只写一句改动指令）。skill=nai 也支持。本机渠道的强度是定死的（重绘 0.6、改图 1），传 denoise 也没用"},
            "seed": {"type": "integer", "description": "生图种子，**只在对方点名要「用某个种子重画 / 换提示词再来一张」时才传**，平时一律不传（不传=随机）。范围 0 ~ 4294967295 的整数，填错格式/超界会直接报错，别猜。种子会跟着编号印在图那行 caption 上（`编号 · 分辨率 · 渠道 · seed 数字`），对方引用那条消息时能一起带回来。⚠️ 同一个种子只有配**同样的提示词 + 同样的渠道 + 同样的 lora**才画得出同一张图（改提示词重画=构图大体在、细节变）；动漫渠道是两段采样、两段共用这一个种子，所以只有这一个数。**只有本机渠道认**（anima_* / hd_* / qwen_image_v1 / image_gen_v1 / krea2 / nffa），skill=nai 传了会被拒；image_gen_v1 一次出多张时第 k 张 = 这个数 + k - 1"}
        },
        "required": ["prompt"]
    }
}
