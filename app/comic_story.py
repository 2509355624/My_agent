"""连续剧情小漫画：一句话设定 → 「固定块 + 动态块 × N」→ 逐格渲染发回。

契约（新，见 skills/storyboard-prompt/skill.md）：

    <固定块：一行，角色标识 + 脸部特征>

    <动态第 1 格>
    ---
    <动态第 2 格>
    ---
    ...

渲染时逐格拼 `固定块 + ", " + 动态[i]`，再用 `---` 连成一份多提示词，**一次提交**
给批量工作流（`skills/_comic_batch`）——一个 ComfyUI 任务出全部 N 张。

与 app/comic.py 的关系：comic.py 走**旧契约**（`## NN` / `画面:` / `气泡:`，
每格原样重复整套角色标签），是 CLI 用的；本模块是**新契约**（固定写一次、
动态只写增量），给 QQ 机器人用。两套并存，互不影响——旧 CLI 一个字没动。

为什么另起一套：旧契约每格重复整套角色标签，多人时容易串味；新契约把
「不变的」抽到固定块、只让「会变的」进动态，这是 skill 的核心。
"""

import logging
import random
import re
import threading

from app import llm

log = logging.getLogger("comic.story")

DEFAULT_PANELS = 10
MAX_PANELS = 30

# 漫画的渲染渠道：`skills/_comic_batch/`，用 `BatchPromptImageGenerator` 把整批
# 提示词在**一个** ComfyUI 任务里跑完（模型只加载一次）。**不是可点名的渠道**
# ——目录名以 `_` 开头，`list_skills()` 会跳过它，AI 点不到。
#
# 为什么不用 `silver`：原生 silver 是「一次一张」的骨架（1024×1536、两段采样、
# 2x 像素放大，实测 ≈48 秒/格）。10 格漫画 = 10 个任务、每格重读一遍权重，
# 合计 ≈480 秒，还把这个会话的并发名额占满。2026-10-08 用户拍板：
# 「你用我原生 silver 渠道去跑？10 个图片要 300 多秒钟，排队排死人」。
COMIC_SKILL = "_comic_batch"

# 编剧提示词：优先读 skill 原文（用户改 skill 就跟着变，单一真相源）；
# 读不到（skill 被删/改名）时退回这段内置的等价骨架。
_FALLBACK_SYSTEM = """你是条漫分镜编剧。把用户给的设定写成一份程序可直接解析的连续分镜。

严格照下面的结构输出，不要加任何解释、标题、编号或代码块围栏：

<固定块：一行，角色标识 + 脸部特征，英文 booru 标签，逗号分隔>

<动态第 1 格>
---
<动态第 2 格>
---
（共 {panels} 段，用 --- 分隔）

硬性要求：
1. 固定块只有一行，写「谁」：人数 + 角色名 + 脸部标志性特征（发色、发型、瞳色、
   发饰、面部标记）。**不写服饰、不写体型**（服饰属于动态部分）。多人写成
   `3girls, girl1, girl2, girl3, girl1 <特征>, girl2 <特征>, ...`。
2. 动态部分每格一行，写「在哪、穿什么、做什么、什么表情、什么镜头、什么光」。
   顺序：场景 + 角色1（服饰+动作+神态）+ 角色2（...）+ 视角 + 灯光。
3. 全部英文 booru 标签：全小写、多词用下划线、逗号分隔、不是句子；
   **绝对不许出现中文、冠词、标点句读**。
4. 服饰每格都要写全颜色+款式，不许泛化（不能只写 dress / skirt / underwear，
   要写 black_plaid_skirt）；同一件衣服每格颜色必须一致。
5. 灯光每格都要写，同一场景保持一致。
6. 多角色：每个角色打包成一个连续短语、只写一次角色名；角色顺序全程不变。
7. 动态段只写增量，不许重复固定块已有的属性。
8. 段数必须正好 {panels} 段。
"""

_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")
_SEP_RE = re.compile(r"^[ \t]*-{3,}[ \t]*$", re.M)
_FRONTMATTER_RE = re.compile(r"^\s*---\s*\n.*?\n\s*---\s*\n", re.S)


def clamp_panels(n):
    """格数收敛：非数字/非正 → 默认 10；超过硬上限 30 → 截到 30。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = DEFAULT_PANELS
    if n <= 0:
        n = DEFAULT_PANELS
    return min(n, MAX_PANELS)


def _clean(text):
    """去掉模型爱加的代码块围栏和首尾空白。"""
    lines = [ln for ln in (text or "").splitlines() if not _FENCE_RE.match(ln)]
    return "\n".join(lines).strip()


def _system_prompt(panels):
    """编剧提示词：优先用 skill 原文（去掉 frontmatter），读不到再退回内置骨架。"""
    body = ""
    try:
        from app import skills
        s = skills.load_skill("storyboard-prompt")
        body = ((s or {}).get("skill_md") or "").strip()
        body = _FRONTMATTER_RE.sub("", body).strip()
    except Exception:                      # skill 层出问题不能挡住编剧
        body = ""
    if not body:
        body = _FALLBACK_SYSTEM
    return body.format(panels=panels) if "{panels}" in body else body


def parse_story(text):
    """把编剧输出解析成 (固定块, [动态块...])。

    固定块 = 第一行非空文本；其余按 `---`（三个及以上短横线）切段，
    空段丢掉。容忍模型多写/少写空行——只认「第一行 = 固定，之后 = 动态」。
    """
    text = _clean(text)
    if not text:
        return "", []
    lines = text.splitlines()
    fixed, start = "", 0
    for i, ln in enumerate(lines):
        if ln.strip():
            fixed = ln.strip()
            start = i + 1
            break
    rest = "\n".join(lines[start:])
    dyns = [d.strip() for d in _SEP_RE.split(rest) if d.strip()]
    return fixed, dyns


def assemble(fixed, dynamics):
    """逐格拼提示词：`固定块 + ", " + 动态[i]`（各去掉首尾逗号，避免 `,,`）。"""
    f = (fixed or "").strip().strip(",").strip()
    out = []
    for d in dynamics:
        d = (d or "").strip().strip(",").strip()
        out.append(f + ", " + d if f else d)
    return out


def _validate(fixed, dynamics, panels):
    """按契约校验，返回 (是否合格, 问题描述)。"""
    if not fixed:
        return False, "缺固定块（第一行必须是角色标识 + 脸部特征）"
    if _CJK.search(fixed):
        return False, "固定块里混进了中文（必须全是英文 booru 标签）"
    if len(dynamics) != panels:
        return False, "只解析到 %d 段动态，要求 %d 段" % (len(dynamics), panels)
    for i, d in enumerate(dynamics, 1):
        if not d.strip():
            return False, "第 %d 格动态是空的" % i
        if _CJK.search(d):
            return False, "第 %d 格动态里混进了中文（必须全是英文 booru 标签）" % i
    return True, ""


def write_story(brief, panels=DEFAULT_PANELS, tries=3):
    """调 LLM 写剧本；不合格就带着错因回炉重写。返回 (固定块, [动态块...])。

    回炉是必需的：模型第一次常把正文写成中文、或段数不对；与其让下游解析出
    一堆空字段，不如在这里卡住重来。**单条 user 消息**（本项目约定不用
    system 角色），回炉时把错因追加进同一条消息。

    只报**格数**，不报画风：漫画只有一条渲染路（见 `COMIC_SKILL`），渠道名
    对编剧没有信息量，写进提示词反而可能被当成标签抄进正文。
    """
    if not (brief or "").strip():
        raise ValueError("剧情不能为空")
    panels = clamp_panels(panels)
    prompt = (_system_prompt(panels)
              + "\n\n格数：%d\n设定：%s" % (panels, brief.strip()))
    why = ""
    for attempt in range(1, tries + 1):
        text = _clean(llm.call_llm([{"role": "user", "content": prompt}]))
        fixed, dyns = parse_story(text)
        ok, why = _validate(fixed, dyns, panels)
        if ok:
            log.info("漫画剧本已生成（第 %d 次尝试，%d 格）", attempt, panels)
            return fixed, dyns
        log.warning("漫画剧本不合格（第 %d 次尝试）：%s", attempt, why)
        prompt += ("\n\n上一次输出不合格：%s。请重新输出**完整**剧本，"
                   "只输出剧本本身，不要解释。" % why)
    raise RuntimeError("连写 %d 次都不合格，最后一次的问题：%s" % (tries, why))


def _new_seed():
    return random.randint(0, 2 ** 32 - 1)


def render(target, target_id, fixed, dynamics):
    """整批渲染并发回会话。**阻塞**——调用方负责把它放进后台线程。

    一次提交：把每格提示词（`assemble()` 拼好的「固定块 + 动态」）用 `---`
    连成一份多提示词，交给批量工作流，**一个 ComfyUI 任务出全部图**。返回要
    画的格数（0 = 连提交都没成）。

    ⚠️ 图是**一次全给**，不是一张一张给：工作流开了 `save_inline`，每张画完就
    落盘，但 ComfyUI 的 history 要等整批跑完才出现 `outputs`，发图由
    `image_jobs.process` 在任务结束时统一做（它本来就支持一个任务多张图）。
    所以回执文案不能说「画好一张发一张」。
    """
    from app import image_jobs
    from app.tools.normal import generate_image as gi

    prompts = assemble(fixed, dynamics)
    total = len(prompts)
    if not total:
        return 0
    # `---` 分隔：和批量工作流的 `delimiter` 默认值、以及本模块的契约同一个符号。
    multi = "\n---\n".join(prompts)
    seed = _new_seed()
    wf = gi.build_t2i_workflow(COMIC_SKILL, multi, seed)
    if wf is None:
        log.error("漫画渠道 %s 没有文生图工作流", COMIC_SKILL)
        return 0
    log.info("漫画开跑：%d 格，渠道 %s，会话 %s %s", total, COMIC_SKILL,
             target, target_id)
    try:
        # landscape=False 写死：漫画是竖版，且后台线程读不到本轮原话
        # （qq_api 的上下文是 threading.local），不能让 enqueue 自己去判。
        job, reason = image_jobs.enqueue(target, target_id, wf,
                                         skill=COMIC_SKILL, prompt=multi,
                                         seed=seed, landscape=False)
        if reason:
            log.warning("漫画被拒：%s", reason)
            return 0
        gi._charge_quota(job, target, target_id)   # 私聊额度（群聊恒空）
        job.wait()                                  # 失败会抛 job.error
    except Exception as e:
        log.error("漫画失败：%s", e)
        return 0
    log.info("漫画收尾：整批 %d 格跑完", total)
    return total


def start(target, target_id, fixed, dynamics):
    """起一个后台线程跑 `render`，立刻返回（工具侧不能阻塞适配层的并发槽）。"""
    t = threading.Thread(target=render,
                         args=(target, target_id, fixed, dynamics),
                         name="comic-render", daemon=True)
    t.start()
    return t
