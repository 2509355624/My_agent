"""Silver 风格连续漫画编剧。

只服务自由视角（free / third）：一次 LLM 请求，直接输出固定块 + 每格画面，
可选带女/男气泡；不查标签库、不走 comic_agent、不走内容补正。
"""

import logging
import re

from app import comic, llm

log = logging.getLogger("comic.silver")

MAX_PANELS = 30
MODES = ("no_bubble", "bubble")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")

_SYSTEM = """你是 silver 风格的连续漫画分镜编剧。

你的任务不是做标签审计，也不是写旁白，而是把用户的剧情写成一组能连续生图的画面。
只调用一次，直接交付完整结果。不要查标签库，不要解释，不要补正，不要输出第二版。

输出格式必须严格如下：

# 标题：中文标题
固定部分：英文角色固定信息

## 01
画面: 英文画面提示词
{bubble_fields}

## 02
画面: 英文画面提示词
{bubble_fields}

固定部分只回答「谁」：角色数量和性别、角色名、发色、发型、瞳色、发饰或面部标志。
固定部分不写服饰、鞋袜、配件、体型、动作、场景、灯光——服饰属于每格动态画面。
固定部分会被原样拼到每一格开头，所以这里只放全篇不变的脸部身份信息。
字段名也可以写成 `固定部分:`，不要改成固定块或其他标题。

每格的画面提示词按这个顺序写：
场景 + 女生服饰 + 男生服饰 + 女生动作 + 男生动作 + 女生神态 + 男生神态 + 视角 + 灯光。

每格必须把每个入画角色的服饰完整写一遍，颜色 + 具体款式都要有，
例如 `girl wearing black lace bra and pink frilled shorts`、
`boy wearing grey t-shirt and black shorts`。
- 每格都要重新写全，不能因为上一格写过就省略。
- 禁止只写 `clothes`、`outfit`、`dress`、`shoes` 这类泛化词。
- 除非剧情明确换装，否则同一件衣服每格的颜色和款式必须完全一致。
- 换装时，换掉的那件也要写清新的颜色和款式。
- 每格只写一遍服饰，不要在同一格画面里重复第二遍。

每格不要再重复固定部分已经写过的角色名、发色、瞳色、发饰：它们会被拼在每格开头，
你只写这一格新增的场景、服饰、动作、神态、视角和灯光。每个动作和神态前要指明是谁，
例如 `girl sitting on bed`、`boy gentle smile`。

构图与服饰必须自洽：近景拍不到的鞋袜不要硬塞进画面，但不能靠删服饰解决，
应调整景别或保留服饰设定；同一个角色的服饰全篇不能凭空消失。

画面写自然、简短、明确的英文提示词，可以使用短语，不要为了凑标签而堆词。不要使用
中文，不要写旁白，不要写标签库查询结果，不要写质量词。标签只是描述故事的辅助语言，
不能反过来决定角色服饰、动作或场景；以连贯、可见、可生成的画面为准。

每格都必须有 `画面:`，画面不要把故事解释成抽象概念，而要写成静态画面中能看见的
状态。动作要让读者看出这一格和上一格的变化。

{bubble_rules}

共输出正好 {panels} 格。只输出上述剧本，不要代码块，不要前言和后记。
"""

_NO_BUBBLE = "这一版没有文字：每格只输出 `画面:`，不要输出 `气泡:`、`气泡2:`、`标签:`。"
_BUBBLE = """这一版需要对话：每格可以有 0~2 句短台词。
- 女生说的写成 `气泡: （女）中文台词` 或 `气泡2: （女）中文台词`。
- 男生说的写成 `气泡: （男）中文台词` 或 `气泡2: （男）中文台词`。
- 没有台词就不写气泡行，不要为了凑数硬加。
- 每句短、口语化，尽量不超过 16 个中文字符。
- 台词只表达此刻人物想说的话，不要用旁白解释画面。
"""


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


def parse(text, panels, mode="no_bubble"):
    """把 silver 字段输出转成旧 comic.py 可消费的 script.md。"""
    text = _clean(text)
    if not text:
        raise ValueError("silver 编剧返回为空")
    lines = text.splitlines()
    title = "连续漫画"
    fixed = ""
    blocks = []
    cur = None
    waiting_fixed = False
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# 标题:"):
            title = line.split(":", 1)[1].strip() or title
        elif re.match(r"^(?:固定部分|固定块|固定角色|角色固定)\s*[:：]?\s*$", line):
            # 兼容模型把字段名单独写一行、下一行才给英文角色串。
            waiting_fixed = True
        elif re.match(r"^(?:固定部分|固定块|固定角色|角色固定)\s*[:：]", line):
            fixed = re.split(r"[:：]", line, maxsplit=1)[1].strip()
            waiting_fixed = not fixed
        elif re.match(r"^##\s*\d+", line):
            if cur is not None:
                blocks.append(cur)
            cur = {"no": re.search(r"\d+", line).group(0),
                   "scene": "", "bubble": "", "bubble2": ""}
        elif waiting_fixed and not cur:
            fixed = line
            waiting_fixed = False
        elif cur is not None:
            if line.startswith("画面:"):
                cur["scene"] = _line_value(line, "画面:")
            elif mode == "bubble" and line.startswith("气泡2:"):
                cur["bubble2"] = _line_value(line, "气泡2:")
            elif mode == "bubble" and line.startswith("气泡:"):
                cur["bubble"] = _line_value(line, "气泡:")
    if cur is not None:
        blocks.append(cur)

    if not fixed:
        raise ValueError("silver 编剧缺固定部分")
    if _CJK_RE.search(fixed):
        raise ValueError("silver 固定部分必须是英文")
    if len(blocks) != panels:
        raise ValueError("silver 编剧输出 %d 格，要求 %d 格" % (len(blocks), panels))

    out = ["# 标题：%s" % title]
    for i, p in enumerate(blocks, 1):
        scene = p["scene"].strip()
        if not scene:
            raise ValueError("silver 编剧第 %d 格缺画面" % i)
        if _CJK_RE.search(scene):
            raise ValueError("silver 编剧第 %d 格画面含中文" % i)
        no = "%02d" % i
        out.extend(["", "## %s" % no, "画面: %s, %s" % (fixed, scene)])
        if mode == "bubble":
            if p["bubble"]:
                out.append("气泡: %s" % p["bubble"])
            if p["bubble2"]:
                out.append("气泡2: %s" % p["bubble2"])
    return "\n".join(out).strip()


def write_script(brief, panels=10, character="", provider=None, model=None,
                 mode="no_bubble", prev=""):
    """一次 API 请求生成并解析自由视角 silver 剧本。"""
    if not (brief or "").strip():
        raise ValueError("设定不能为空")
    if mode not in MODES:
        mode = "no_bubble"
    panels = _clamp_panels(panels)
    fixed_hint = ("\n用户提供的固定角色信息：%s" % character.strip()
                  if (character or "").strip() else "")
    previous = ("\n上一集前情（只用于接续，不要重演）：\n%s" % prev.strip()
                if (prev or "").strip() else "")
    system = _SYSTEM.format(
        panels=panels,
        bubble_fields=("气泡: ...\n气泡2: ..." if mode == "bubble" else ""),
        bubble_rules=_BUBBLE if mode == "bubble" else _NO_BUBBLE)
    user = "格数：%d\n设定：%s%s%s" % (panels, brief.strip(), fixed_hint, previous)
    provider = (provider or "").strip() or llm.CURRENT_PROVIDER or None
    raw = llm.call_llm([{"role": "system", "content": system},
                        {"role": "user", "content": user}],
                       provider=provider, model=model)
    result = parse(raw, panels, mode=mode)
    log.info("silver 自由视角剧本完成（%d 格，mode=%s，一次 API）", panels, mode)
    return result
