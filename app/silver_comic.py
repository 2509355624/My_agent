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

MAX_PANELS = 30
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

_CONTRACT = """【本项目输出契约 —— 排版以下面为准，内容规则仍按上面的规范】

# 标题：中文标题
固定部分一行英文

动态第 1 格一行英文
---
动态第 2 格一行英文

- 固定部分只写一行：角色数量与性别、角色名、发色、发型、瞳色、发饰或面部标志。
  不写服饰、鞋袜、配件、体型、动作、场景、灯光。它会被原样拼到每一格开头。
- 每一行动态部分按上面的规范写：场景 + 女生服饰 + 男生服饰 + 动作 + 神态 + 视角 + 灯光。
  服饰的颜色 + 款式必须写全，每格重复，同一件衣服全篇一致，换装时写清新的颜色和款式。
- 动态部分不要再重复固定部分已有的角色名、发色、瞳色、发饰。
- 动态部分之间只用 --- 分割，不要写 ## 01，不要写「画面:」前缀。
- 共 {panels} 格。只输出标题、固定部分和动态部分，不要代码块、前言、后记。
"""

_BUBBLE_CONTRACT = """- 这一版有台词：每格动态行下面紧跟一行 `气泡: （女）中文台词`
  或 `气泡2: （男）中文台词`，不要空行，不要另起一格。每句不超过 16 个中文字。
"""

_FIXED_LINE_RE = re.compile(r"^(?:固定部分|固定块|固定角色|角色固定)\s*[:：]\s*(.*)$")
_FIXED_ONLY_RE = re.compile(r"^(?:固定部分|固定块|固定角色|角色固定)\s*[:：]?\s*$")
_SEP_RE = re.compile(r"^-{3,}\s*$")
_PANEL_NO_RE = re.compile(r"^##\s*\d+\s*$")


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


def split(text):
    """把 silver 输出拆成 (title, fixed, panels)。

    兼容两种写法：
    - skill 原生：固定部分一行（无字段名）+ 空行 + 动态行 + `---` 分隔；
    - 带字段名：`固定部分:` / `固定块:`，以及旧的 `## 01` + `画面:`。
    """
    text = _clean(text)
    if not text:
        raise ValueError("silver 编剧返回为空")

    title = "连续漫画"
    fixed = ""
    blocks = []
    cur = None
    waiting_fixed = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# 标题:") or line.startswith("# 标题："):
            title = re.split(r"[:：]", line, maxsplit=1)[1].strip() or title
            continue
        if _FIXED_ONLY_RE.match(line):
            waiting_fixed = True
            continue
        m = _FIXED_LINE_RE.match(line)
        if m:
            fixed = m.group(1).strip()
            waiting_fixed = not fixed
            continue
        if _PANEL_NO_RE.match(line) or _SEP_RE.match(line):
            if cur is not None:
                blocks.append(cur)
            cur = {"scene": "", "bubble": "", "bubble2": ""}
            continue
        if waiting_fixed and not fixed:
            fixed = line
            waiting_fixed = False
            continue
        if cur is None:
            if not fixed:
                fixed = line          # skill 原生格式：第一行就是固定部分
                continue
            cur = {"scene": "", "bubble": "", "bubble2": ""}
        if line.startswith("气泡2:"):
            cur["bubble2"] = _line_value(line, "气泡2:")
        elif line.startswith("气泡:"):
            cur["bubble"] = _line_value(line, "气泡:")
        elif line.startswith("画面:"):
            cur["scene"] = _line_value(line, "画面:")
        elif not cur["scene"]:
            cur["scene"] = line       # 动态行没有「画面:」前缀
    if cur is not None:
        blocks.append(cur)

    blocks = [b for b in blocks if b["scene"] or b["bubble"] or b["bubble2"]]
    if not fixed:
        raise ValueError("silver 编剧缺固定部分")
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
        raise ValueError("silver 编剧输出 %d 格，要求 %d 格" % (len(blocks), panels))
    out = ["# 标题：%s" % title]
    for i, p in enumerate(blocks, 1):
        out.extend(["", "## %02d" % i, "画面: %s, %s" % (fixed, p["scene"])])
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
    user = "格数：%d\n设定：%s%s%s" % (panels, brief.strip(), fixed_hint, previous)
    provider = (provider or "").strip() or llm.CURRENT_PROVIDER or None
    raw = llm.call_llm([{"role": "system", "content": system},
                        {"role": "user", "content": user}],
                       provider=provider, model=model)
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
