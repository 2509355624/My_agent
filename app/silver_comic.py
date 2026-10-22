"""Silver 风格连续漫画编剧。

只服务自由视角（free / third）：一次 LLM 请求，system 里直接喂
`skills/storyboard-prompt/skill.md` 全文，模型按「固定部分 + --- + 动态部分」输出；
本模块只做格式解析，不查标签库、不走 comic_agent、不走内容补正。

输出契约（供下游 comic.py 消费）仍然是 script.md：
每格 `画面:` = 固定部分 + ", " + 该格动态部分。
`split()` 另外把固定部分与逐格动态部分单独留出来，给界面分栏看。
"""

import logging
import os
import re

from app import comic, llm

log = logging.getLogger("comic.silver")

MAX_PANELS = 60
MODES = ("no_bubble", "bubble")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")

# skill 原文：用户明确要求「直接把这份 skills 给 AI」，不要自己改写规则。
_SKILL_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "storyboard-prompt", "skill.md")
_skill_cache = {"mtime": None, "text": ""}

# 兜底：skills 目录是 gitignore 的，别人机器上可能没有；这时退回精简规则，
# 保证自由视角还能出稿（关键条目与 skill 一致）。
_FALLBACK_SKILL = """# 连续剧情提示词生成规范

固定部分只写角色标识 + 脸部特征（发色、发型、瞳色、发饰），不写服饰。
每格动态部分写：场景 + 女生服饰 + 男生服饰 + 动作 + 神态 + 视角 + 灯光。
服饰颜色 + 款式必须写全，每格重复，禁止 clothes / outfit / dress / shoes 这类泛化词；
同一件衣服每格颜色款式一致，换装时才改，改后也要写清新颜色和款式。
每个动作、神态前要指明是谁（girl ... / boy ...）。
灯光每格都写，同一场景保持一致。
"""

_CONTRACT = """【输出格式 —— 必须逐字照这个结构写，这是硬性要求，违反整份作废】

# 标题：中文标题
（固定部分：一个角色一行，全英文）
（空一行）
（动态部分第 1 格一行英文）
---
（动态部分第 2 格一行英文）
……共 {panels} 格

═══ 一、固定部分：一个角色一行，每个角色都要写 ═══

这一节最容易写错，逐条照做：
1. 有几个角色就写几行；两个人就是两行，**两行都必须写**，缺一个就是不合格。
2. 每行以人数与性别开头：`1girl,` / `1boy,`。
3. 每行紧接着写：角色名 + 发色 + 发型 + 瞳色 + 发饰（有的话）。这个角色的脸部
   特征必须写全，不能只写名字。
4. **全英文**，不许中文。
5. 只写脸：不写服饰、鞋袜、配件、体型、动作、场景、灯光。
6. 写完空一行，再写动态部分。

正确（双人，两行，两个人都写了）：
1girl, Illyasviel von Einzbern, silver-white long hair, blunt bangs, red eyes, purple hairband with white frills
1boy, faceless man, short black hair, pale skin, grey eyes

正确（单人，只有一行）：
1girl, Illyasviel von Einzbern, silver-white long hair, blunt bangs, red eyes

错误（出现任意一条即不合格）：
固定部分（一行，角色标识 + 脸部特征）        ← 这是文档模板字，不是内容，不要抄
1girl, 1boy, Illyasviel, silver-white long hair  ← 只写了女生的特征，男生没了
女主：伊莉雅，银白长发，红瞳                   ← 中文，必须英文
1girl, Illyasviel, 1boy,                       ← 男生只写了人数，没写特征
1girl, Illyasviel, wearing white sweater       ← 固定部分不许写服饰
固定部分：1girl, ...                          ← 不要写「固定部分：」这个标签，直接写内容行

═══ 二、动态部分：每格一行 ═══

- 每格按顺序写：场景 + 女生服饰 + 男生服饰 + 动作 + 神态 + 视角 + 灯光。
- 服饰的颜色 + 款式必须写全，每格重复，同一件衣服全篇一致，换装时写清新的颜色和款式。
- 不要再重复固定部分已有的角色名、发色、瞳色、发饰（它们会被拼到每格开头）。
- 格与格之间只用 --- 分割，不要写 ## 01，不要写「画面:」前缀。

最后一句话：共 {panels} 格，只输出标题、固定部分、动态部分，不要代码块、前言、后记。
"""

_BUBBLE_CONTRACT = """- 这一版有台词：每格动态行下面紧跟一行 `气泡: （女）中文台词`
  或 `气泡2: （男）中文台词`，不要空行，不要另起一格。每句不超过 16 个中文字。
"""

_SEP_RE = re.compile(r"^-{3,}\s*$")
_PANEL_NO_RE = re.compile(r"^##\s*\d+\s*$")
# 固定部分「一个角色一行」：行首是 1girl / 1boy / 2girls ...
_COUNT_START_RE = re.compile(r"^\d+\s*(?:girls?|boys?|others?)\b", re.I)
_COUNT_HEAD_RE = re.compile(r"^(\d+\s*(?:girls?|boys?|others?))\s*[,，]\s*(.+)$",
                            re.I)
_ASCII_RE = re.compile(r"[A-Za-z]")
# 中文标签行：`# 标题：X`、`固定部分（一行，角色标识 + 脸部特征）`、`**固定部分**：X` 都算。
# 模型会把 skill 里那行模板原样抄出来，这行是标题不是内容，必须识别掉，
# 否则它会被当成固定部分 → 「固定部分必须是英文」/「缺固定部分」两种报错。
_LABEL_RE = re.compile(
    r"^\**\s*#*\s*(标题|固定部分|固定块|固定角色|角色固定)\s*\**\s*"
    r"(?:[（(][^）)]*[）)])?\s*[:：]?\s*(.*)$")


def _skill_text():
    """读 skill.md 原文，按 mtime 缓存（改了 skill 不用重启）。"""
    try:
        mtime = os.path.getmtime(_SKILL_PATH)
        if _skill_cache["mtime"] != mtime:
            with open(_SKILL_PATH, "r", encoding="utf-8") as f:
                _skill_cache["text"] = f.read()
            _skill_cache["mtime"] = mtime
        return _skill_cache["text"]
    except OSError:
        log.warning("读不到 %s，退回落精简规则", _SKILL_PATH)
        return _FALLBACK_SKILL


def _clamp_panels(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 10
    return max(1, min(MAX_PANELS, n))


def _clean(text):
    return "\n".join(x for x in (text or "").splitlines()
                     if not _FENCE_RE.match(x)).strip()


def _line_value(line, key):
    if line.strip().startswith(key):
        return line.split(":", 1)[1].strip()
    return ""


def _system(panels, mode):
    parts = [_skill_text().strip(), "", _CONTRACT.replace("{panels}", str(panels))]
    if mode == "bubble":
        parts.insert(-1, _BUBBLE_CONTRACT)
    return "\n".join(parts)


def _merge_fixed(fixed):
    """多行固定部分合并成一行（每格画面是单行）。

    每行都以 `1girl, ...` / `1boy, ...` 开头时，把人数据前置、各自特征依次排开：
    `1girl, X, 特征..., 1boy, Y, 特征...` → `1girl, 1boy, X, 特征..., Y, 特征...`
    这样既保留了每个角色各自的特征，也符合 skill 里「人数在前」的写法。
    """
    lines = [x.strip() for x in (fixed or "").splitlines() if x.strip()]
    if len(lines) <= 1:
        return lines[0] if lines else ""
    hits = [_COUNT_HEAD_RE.match(x) for x in lines]
    if all(hits):
        counts = [m.group(1) for m in hits]
        rest = [m.group(2).strip() for m in hits]
        return ", ".join(counts + rest)
    return ", ".join(lines)


def split(text):
    """把 silver 输出拆成 (title, fixed, panels)。

    兼容两种写法：
    - skill 原生：固定部分一行或多行（每个角色一行，无字段名）+ 空行 + 动态行 + `---`；
    - 带字段名：`固定部分:` / `固定块:`，以及旧的 `## 01` + `画面:`。

    固定部分的结束信号：空行、`---`、`## NN`，或出现不像身份行的内容。
    多角色分行时，只有行首是 `1girl` / `1boy` 这类身份行才会被并入固定部分，
    避免模型漏写空行时把第一格动态画面吞掉。
    """
    text = _clean(text)
    if not text:
        raise ValueError("silver 编剧返回为空")

    title = "连续漫画"
    fixed_lines = []
    collecting = False        # `固定部分:` 单独一行后，后续行都算固定部分
    closed = False            # 固定部分已结束（遇到空行 / 分隔 / 已经开始写格）
    blocks = []
    cur = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if fixed_lines and not closed:
                collecting = False
                closed = True
            continue
        if _PANEL_NO_RE.match(line) or _SEP_RE.match(line):
            collecting = False
            closed = True
            if cur is not None:
                blocks.append(cur)
            cur = {"scene": "", "bubble": "", "bubble2": ""}
            continue
        m = _LABEL_RE.match(line)
        if m:
            key = m.group(1)
            rest = m.group(2).strip().strip("*").strip()
            if key == "标题":
                title = rest or title
                continue
            if _ASCII_RE.search(rest):        # `固定部分: 1girl, ...`
                fixed_lines.append(rest)
                collecting = False
                closed = True
            else:                             # 只有标签，正文在下一行
                collecting = True
            continue
        if cur is None:
            if not fixed_lines:
                fixed_lines.append(line)      # skill 原生格式：第一行就是固定部分
                continue
            if not closed and (collecting or _COUNT_START_RE.match(line)):
                fixed_lines.append(line)      # 多角色分行
                continue
            collecting = False
            closed = True
            cur = {"scene": "", "bubble": "", "bubble2": ""}
        if line.startswith("气泡2:"):
            cur["bubble2"] = _line_value(line, "气泡2:")
        elif line.startswith("气泡:"):
            cur["bubble"] = _line_value(line, "气泡:")
        elif line.startswith("画面:"):
            cur["scene"] = _line_value(line, "画面:")
        elif not cur["scene"]:
            cur["scene"] = line               # 动态行没有「画面:」前缀
    if cur is not None:
        blocks.append(cur)

    # 纯中文的行（漏网的标签 / 模板占位）不是角色信息，直接丢掉，不要拿它当固定部分。
    fixed_lines = [x for x in fixed_lines if _ASCII_RE.search(x)]
    fixed = "\n".join(fixed_lines).strip()
    blocks = [b for b in blocks if b["scene"] or b["bubble"] or b["bubble2"]]
    if not fixed:
        log.warning("silver 原文没有可用固定部分，前 300 字：%r", text[:300])
        raise ValueError(
            "silver 编剧缺固定部分：模型没按格式输出，"
            "固定部分应是 1girl/1boy 各一行英文脸部特征；请重新生成")
    if _CJK_RE.search(fixed):
        raise ValueError("silver 固定部分必须是英文")
    for i, b in enumerate(blocks, 1):
        if not b["scene"]:
            raise ValueError("silver 编剧第 %d 格缺画面" % i)
        if _CJK_RE.search(b["scene"]):
            raise ValueError("silver 编剧第 %d 格画面含中文" % i)
    return title, fixed, blocks


def parse(text, panels, mode="no_bubble"):
    """转成下游 comic.py 消费的 script.md：每格 = 固定部分 + 该格动态部分。"""
    title, fixed, blocks = split(text)
    if len(blocks) != panels:
        # 少格多数是被输出上限截断（服务端 max_tokens 不够 / 中转站偷偷限流），
        # 说清楚是「少了」还是「多了」，别让人以为是编剧乱写。
        hint = "（少了，通常是输出被截断：换官方渠道或减少格数）" if len(blocks) < panels else ""
        raise ValueError("silver 编剧输出 %d 格，要求 %d 格%s"
                         % (len(blocks), panels, hint))
    flat = _merge_fixed(fixed)
    out = ["# 标题：%s" % title]
    for i, p in enumerate(blocks, 1):
        out.extend(["", "## %02d" % i, "画面: %s, %s" % (flat, p["scene"])])
        if mode == "bubble":
            if p["bubble"]:
                out.append("气泡: %s" % p["bubble"])
            if p["bubble2"]:
                out.append("气泡2: %s" % p["bubble2"])
    return "\n".join(out).strip()


def write_script_detail(brief, panels=10, character="", provider=None, model=None,
                        mode="no_bubble", prev=""):
    """一次 API 请求，返回 {title, fixed, panels, script} 给接口和界面用。"""
    if not (brief or "").strip():
        raise ValueError("设定不能为空")
    if mode not in MODES:
        mode = "no_bubble"
    panels = _clamp_panels(panels)
    fixed_hint = ("\n用户提供的固定角色信息：%s" % character.strip()
                  if (character or "").strip() else "")
    previous = ("\n上一集前情（只用于接续，不要重演）：\n%s" % prev.strip()
                if (prev or "").strip() else "")
    system = _system(panels, mode)
    # 固定部分的形状在 user 里再钉一遍：system 里塞着整份 skill，长上下文里最硬
    # 的约束容易被冲淡（2026-10-22 用户 50 格时实报「缺固定部分」）。
    user = ("格数：%d\n设定：%s%s%s\n\n"
            "【输出格式硬性要求】\n"
            "第 1 行：# 标题：中文标题\n"
            "接着是固定部分，一个角色一行，全英文，先人数再角色名再脸部特征：\n"
            "1girl, 角色名, 发色, 发型, 瞳色, 发饰\n"
            "1boy, 角色名, 发色, 发型, 瞳色\n"
            "（有两个角色就必须写两行，两个人都要写全；固定部分不写服饰）\n"
            "空一行，然后是动态部分：正好 %d 格，每格一行英文，格之间用 --- 分隔，\n"
            "不要写 ## 01，不要写「画面:」，不要解释、不要编号、不要目录。"
            % (panels, brief.strip(), fixed_hint, previous, panels))
    provider = (provider or "").strip() or llm.CURRENT_PROVIDER or None
    # 输出上限按格数放大（2026-10-22）：50 格约 3500+ token，服务端默认 4096 会在
    # 半句话上截断，表现出来就是「输出 47 格，要求 50 格」或固定部分干脆丢了。
    # 160 是实测每格一行英文标签的宽松估算，至少给 4096。
    budget = max(4096, panels * 160)
    # 超时也必须跟着放大：全局 LLM_REQUEST_TIMEOUT 是 60 秒（对话抢答的口径），
    # 50 格一次出完要 90 秒以上，用 60 秒必超时——超时会顺着降级链换家重试，
    # 最后报「全挂了」，用户看到的就是「50 页根本没法生成」。下限 120 秒，
    # 每格 8 秒，最多 10 分钟（再久就该考虑分两批写了）。
    timeout = max(120.0, min(600.0, panels * 8.0))
    raw = llm.call_llm([{"role": "system", "content": system},
                        {"role": "user", "content": user}],
                       provider=provider, model=model,
                       timeout=timeout, max_tokens=budget)
    title, fixed, blocks = split(raw)
    script = parse(raw, panels, mode=mode)
    log.info("silver 自由视角剧本完成（%d 格，mode=%s，一次 API）", panels, mode)
    return {"title": title, "fixed": fixed, "panels": blocks, "script": script}


def write_script(brief, panels=10, character="", provider=None, model=None,
                 mode="no_bubble", prev=""):
    """一次 API 请求生成并解析自由视角 silver 剧本（只返回 script.md）。"""
    return write_script_detail(
        brief, panels=panels, character=character, provider=provider,
        model=model, mode=mode, prev=prev)["script"]
