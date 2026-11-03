"""moss 系提示词清洗：删掉从别家抄来的 booru 习惯词。

## 为什么需要这一步

QQ 侧生图有一套通用写法（`agents/qq/image_guide.md` §六）：质量词 + 人数 tag +
标签串 + 权重语法。49 个渠道里 43 个是 Qwen 系语言编码器、6 个是 SD 系，
这套写法在它们身上都成立。

moss 不在其中：它是 Qwen3.5-9B **语言**编码器（`skills/moss-image-prompt/SKILL.md`
§5「单向因果染色定律」），读的是分块自然语言。对它来说

- `masterpiece, best quality, score_7` 不加分、只挤占前段注意力（而前段正是
  写「谁在做什么」的位置）；
- `(tag:1.2)` 这种权重语法会被当**字面文本**塞进编码器；
- `censored` / `watermark` / `cosplay` 在正向里出现等于主动要求画出来
  （规范 §1 必删 B/C）。

2026-11-04 实测：群友把第三方 bot 的提示词整段抄进 moss，两 girl 同框那张
姿态全错（说好的「一人躺一人站」画成两人都趴着）——属性全挤在一条标签串里，
「the other girl」绑不到具体的人。写法问题归写法（见 image_guide §七），
但垃圾串至少可以确定地删掉。

## 这里的边界

只做**能确定判定**的字符串清洗，不看画面内容、不判断写得好不好——那部分
交给模型。判据只有一张固定词表和一个正则，删不掉的词一律不碰。
"""

import re

# 质量词：booru 系模型靠它们把画质拉起来，moss 的模板前缀与 LoRA 触发词已经
# 接管了这件事，再写一遍只是稀释。`score_N` 是 NAI 体系专有的评分词。
_QUALITY = (
    "masterpiece", "best quality", "high quality", "highest quality",
    "top quality", "ultra quality", "amazing quality", "good quality",
    "normal quality", "absurdres", "highres", "high resolution",
    "very aesthetic", "aesthetic", "newest", "recent",
    "ultra detailed", "extremely detailed", "ultra-detailed",
    "ultra hd", "uhd", "hdr", "8k wallpaper", "4k wallpaper",
    "8k", "4k", "2k",
)

# 规范 §1 必删：打码与退化词、Coser 假人感。它们在正向里会被当成画面要求。
_BANNED = (
    "censored", "mosaic censoring", "bar censor", "uncensored",
    "watermark", "signature", "patreon", "patreon username",
    "cosplay", "cosplayer", "cosplay wig",
)

_WEIGHT_RE = re.compile(r"\(\s*([^()]*?)\s*:\s*\d+(?:\.\d+)?\s*\)")
_SCORE_RE = re.compile(r"\bscore_\d+(?:\.\d+)?\b", re.I)
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def is_moss(skill):
    """这条渠道是不是 moss 系（`moss-qunyou` / `moss-light` …）。

    只认 `moss` 本身和 `moss-` 前缀：以后再加 `moss-xxx` 变体不用回来补词表，
    而 `mossy` 这种只是开头的别的渠道不会被误伤。
    """
    s = str(skill or "").strip().lower()
    return s == "moss" or s.startswith("moss-")


def _strip_words(text):
    """删固定词表。按整词边界删，`high quality` 不会误伤 `quality time`。

    `8k` / `4k` 这类数字词不能用 `\\b`（`8k` 后面接中文时边界判定不稳），
    单独走一条前后加空格比对的路径。
    """
    out = " " + text + " "
    for w in sorted(_QUALITY + _BANNED, key=len, reverse=True):
        # 前后都要求不是字母数字，避免 `masterpieces` 或 `cosplayed` 被切开
        out = re.sub(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(w),
                     ",", out, flags=re.I)
    return out.strip()


def strip_noise(prompt):
    """删质量词 / 权重语法 / 打码词，返回清理后的提示词。

    **按行处理、不合并行**：moss 的分块写法（`<|left_subject|>…<|/left_subject|>`）
    靠换行隔离，一旦把整段拍平，块结构就没了。每行内部再按逗号切、丢掉变空的
    片段、用 `, ` 拼回去——`On the left, a girl sits on the rock.` 这种句子
    切完再拼完全等价，句号留在原处。
    """
    text = str(prompt or "")
    if not text.strip():
        return text
    # 权重语法先展开成裸标签：`(smile:1.2)` → `smile`，别让括号进编码器
    text = _WEIGHT_RE.sub(r"\1", text)
    text = _SCORE_RE.sub(",", text)
    text = _strip_words(text)

    lines = []
    for raw_line in text.splitlines():
        segs = []
        for seg in re.split(r"[,，]", raw_line):
            seg = _MULTI_SPACE.sub(" ", seg).strip()
            if seg.strip(",，. "):
                segs.append(seg)
        line = ", ".join(segs).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def clean(skill, prompt):
    """moss 渠道才清洗，其余渠道原样返回。

    调用点只有一处（`generate_image` 拼 nai5 前缀之前），所以这里判断渠道，
    不要求调用方每次都记得先问 `is_moss`。
    """
    if not is_moss(skill):
        return prompt
    cleaned = strip_noise(prompt)
    # 洗空了就退回原文：空提示词是「随便画一张」，比重画一次还糟
    return cleaned or prompt
