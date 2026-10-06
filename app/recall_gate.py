"""反推闸门：对方在要「看图反推提示词」时代码直接调识图，不走模型自由发挥。

## 它解决什么

对方发一张图（或引用一张图）说「反推提示词 / 这张用的什么提示词」，模型
本该**看图**现推一段，实际却经常**把历史里上次生图的提示词原样搬出来交差**
（2026-10-04 用户实测：给了明确指令有时也不遵守）。病根与 `_hd_tier_guard`
一脉相承——9B 对「别偷懒」这类规则是概率执行的，**判据必须代码判**。

## 闸门怎么判

`decide()` 只在 QQ 轮里被调（`_run_turn` 挂载点），三个条件**全中**才接管：

1. **意图**：本轮对方自己打的话命中**显式**反推动词（反推 / 图生文 /
   「提取提示词」…）。2026-10-06 起**只认显式动词**——原先那条「提示词 +
   给我/是什么」的组合判据随 `_prompt_ask_guard` 一起删了，它只认关键词、
   会把改图请求吞成反推。
2. **有图**：本轮出现的图（引用优先，其次他自己发的）已经转成了 data URL。
3. **不是自家图**：全文里出现 HT 图号 = 引用的是机器人自己画的图，账本
   （recall_image）里存着**当时真正的提示词**，比看图现推准得多——不接管，
   放行走 recall_image。

接管后：逐张调识图（vision.describe，走管理页选的识图模型），把反推结果
拼成回复直接发回去，**模型整轮不参与**。识图失败 → 返回 None 原样放行，
让正常流程（它自己那份识图注入 + 兜底文案）接手，闸门绝不把轮子弄丢。

## 刻意不做的

- **不写会话历史**：反推是一次性的取数服务，不走 agent 循环就没有落盘点；
  硬往 history 里手写 user/assistant 两条会踩多模态内容的格式雷。代价是
  紧跟着的「再画一张」引用不到这轮——真遇到再补。
- **不接管主动接话轮**（voluntary）：接话轮的「原话」是机器人自己的
  INTERJECT_PROMPT，不是用户需求，拿来判意图是张冠李戴。
"""

import logging

log = logging.getLogger(__name__)

# 显式反推动词。别顺手往里加「看」——「看看这张图」可能只是想聊天点评。
_REVERSE_RE = None  # 惰性编译，见 _intent()


def _intent(text):
    """本轮原话是不是在要「看图反推」。命中返回命中的词，否则 None。"""
    global _REVERSE_RE
    if not text:
        return None
    if _REVERSE_RE is None:
        import re
        _REVERSE_RE = re.compile(
            r"反推|提取.{0,4}提示词|还原.{0,4}提示词|转成?提示词|图片转提示词"
            r"|图生文|识别(一?下)?(这|此|那张|这张)图")
    m = _REVERSE_RE.search(text)
    if m:
        return m.group(0)
    # ⚠️ 这里原先还有一条「组合判据」：名词（提示词/词条/种子）+ 要文本的信号
    # 词（给我/是什么/看看/停…）就判定在要词条、直接看图反推——与已删除的
    # `_prompt_ask_guard` 同一套判据。2026-10-06 随那道闸一起删：它同样只认
    # 关键词，把「引用这张图，手势改成抓手，角色换成花火」这类**改图请求**吞成
    # 「反推提示词」。显式反推动词（上面那条）是对方的明确指令，留着；猜意图
    # 的活交回模型。
    return None


# 反推专用识图指令。直接整段替换识图 prompt（不走 build_prompt 的默认描述
# 模板——那套是「看图说话」，这里要的是「看图写出可用的生图提示词」）。
_RECALL_PROMPT = (
    "你在帮用户「反推提示词」：看这张图，写出能生成近似画面的生图提示词。\n"
    "要求：\n"
    "1. 主体与外貌特征优先：人物写发色/发型/瞳色/表情/服装/姿势/构图视角，"
    "非人物写物体与场景要点。\n"
    "2. 用英文 Danbooru 风格 tag，逗号分隔，按重要性排序；"
    "best quality 之类画质词不用写。\n"
    "3. tag 之后另起一行，用一句中文概括画面。\n"
    "4. 只输出反推结果，不要寒暄，不要说做不到。"
)

# 一轮最多反推几张：引用加本体有时会攒出四五张，全描述又慢又刷屏。
_MAX_IMAGES = 3

_HEADER = "这张图的提示词反推如下，可以直接用，也可以改："


def _describe_all(data_urls):
    """逐张识图反推，拼成一条回复。全部失败返回 None。"""
    from app.vision import describe

    outs = []
    for i, data_url in enumerate(data_urls[:_MAX_IMAGES], 1):
        try:
            text = (describe(data_url, prompt=_RECALL_PROMPT) or "").strip()
        except Exception:
            log.exception("反推识图失败（第 %d 张）", i)
            continue
        if not text:
            continue
        if len(data_urls[:_MAX_IMAGES]) > 1:
            outs.append("（第 %d 张）%s" % (i, text))
        else:
            outs.append(text)
    return "\n\n".join(outs) if outs else None


def decide(own_text, full_text, data_urls, voluntary=False):
    """反推闸门入口。接管返回回复文本；不接管 / 识图失败返回 None。"""
    if voluntary:
        return None
    text = (own_text or "").strip()
    if not text or not data_urls:
        return None
    hit = _intent(text)
    if not hit:
        return None
    # 自家图有账本：HT 图号在**全文**里找（引用块正文才是它出现的地方）。
    from app import image_log
    if image_log.find_tags(full_text or text):
        return None
    log.info("反推闸门接管：原话命中 %r，本轮有 %d 张图，代码直调识图",
             hit, len(data_urls))
    body = _describe_all(data_urls)
    if body is None:
        log.info("反推识图没拿到结果，放行正常流程")
        return None
    return _HEADER + "\n" + body
