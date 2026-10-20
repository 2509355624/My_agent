"""连续小漫画工具：一句话剧情 → N 格连续分镜 → 逐格渲染发回会话。

触发形态（用户在 QQ 里说）：`大大怪，<画风>漫画，<数量>，<剧情>`
例：`大大怪，silver漫画，10个，原神的雷电将军在樱花树下和旅行者相遇`

工具只做三件事：
  ① 把剧情交给编剧（app/comic_story.write_story）写出「固定块 + 动态块 × N」；
  ② 起一个后台线程整批渲染（app/comic_story.start），**不阻塞**适配层并发槽；
  ③ 直发一句回执，让模型闭嘴（别复述「稍等 / 马上好」，图会自己发出来）。

渲染渠道有两条（`comic_story.COMIC_SKILLS`，一次提交出全部 N 张）：
  - `silver`（默认）：连续小漫画，默认 10 格
  - `anima`：「anima多图」，anima 配方，默认 3 格
style 由 `direct_gen` 从原话判定后透传（QQ 侧），工具路径默认 silver。硬上限 30。
"""

import logging

from app import comic_story, image_jobs
from app.tools.normal import generate_image as gi

log = logging.getLogger("tool.comic")


def _generate_comic(brief, panels=None, style="silver", landscape=None):
    """brief = 剧情/角色描述；panels = 格数（按 style 取默认）；style = 渠道。

    landscape 不传（None）= 按本轮原话自动判「横屏 / 横版 / 横图」。**必须在这
    一层拍板**：真正的渲染在 `comic_story.render` 里、跑在后台线程，那时
    `qq_api` 的 threading.local 已经不在本轮了（`turn_is_landscape` 恒 False）。
    """
    # 生图总闸 + 单群闸 + 私聊额度闸（网页端不受限）。漫画复用同一套闸。
    gate = gi._qq_gate()
    if gate is not None:
        return gate
    # ⚠️ 快照要放在**写剧本之前**：`write_story` 要跑好几秒的 LLM 调用，虽然
    # 同在会话线程、上下文还绑着，但把判据尽早钉死更稳（万一日后挪进线程）。
    if landscape is None:
        landscape = image_jobs.turn_is_landscape()

    from app import qq_api
    target, target_id = qq_api.current_context()
    if target is None:
        return "错误：连续漫画目前只在 QQ 里画，网页端用不了。"

    style = (style or "silver").strip() or "silver"

    # 群聊不画连续漫画（2026-10-20 用户要求：「帮我群聊禁止使用 silver 漫画，
    # 不然容易刷屏」）。silver 批量一次默认 **10 格**，群里连着发就是一屏图。
    # 范围只到「群聊 + silver」：
    #   · 私聊照旧（一对一，发 10 张不算刷屏，也是用户要的完整剧情）；
    #   · 网页端漫画台不受影响（target is None 时上面已经返回了）；
    #   · 「anima多图」默认 3 格，量小，这次没禁（用户只说 silver 漫画）。
    # 位置钉在 `write_story` **之前**：那一步要跑好几秒 LLM，先拒能省掉整轮开销。
    # 文案按 `_qq_gate` 的惯例返回一句模型能直接转述的话，不抛错。
    if style == "silver" and target == "group":
        return ("错误：群里不画连续漫画，一次十张会把群刷屏，这个群只能画单张。"
                "这一轮别再重试、也别自己改画十张单图；"
                "直接告诉对方：想要图就点名单张渠道一张一张来"
                "（「silver <需求>」「qwen <需求>」都行）；"
                "真要画整段剧情，让他私聊发。")

    n = comic_story.clamp_panels(panels,
                                 default=comic_story.default_panels(style))

    try:
        fixed, dyns = comic_story.write_story(brief, panels=n)
    except Exception as e:
        # 剧本没写出来 = 一张都没画，别让模型编「稍等」。说清楚，别重试。
        return ("错误：漫画剧本没写出来（%s），这一单一张都没画。"
                "直接告诉对方这次没成、别重试。" % e)

    comic_story.start(target, target_id, fixed, dyns, style=style,
                      landscape=landscape)

    # 批量模式是**一次全给**，拿不到中间产物（见 comic_story.render 的注释），
    # 所以不能说「画好一张发一张」。
    if style == "anima":
        receipt = "多图已开工：%d 张，正在画，画完一起发。" % len(dyns)
    else:
        receipt = "漫画已开工：%d 格，正在画，画完一起发。" % len(dyns)
    # ⚠️ 判据必须走 `gi._send_receipt`（内部 try/except + 返回 True/False）。
    # **别直接判 `image_jobs._send_text(...)` 的返回值**——那个函数没有 return
    # 语句、恒返回 None，拿它当判据永远判假 → 回执会「直发一遍 + 再走 fallback
    # 发一遍」。2026-10-08 实录：私聊 2509355624 收到两条一模一样的「漫画已开工」。
    if gi._send_receipt(target, target_id, receipt):
        return (image_jobs.RECEIPT_SENT_MARK + receipt + "\n"
                "（上面那句系统已经直接发到会话里了：**不要再复述张数，"
                "也不要说「稍等 / 马上好」**，本轮别再为这件事说什么。）")
    # 直发失败才退回「由模型转述」的老文案，至少不会一声不响。
    return (receipt + "不要输出图片地址，也不要说「图在下面 / 稍等」，"
            "直接把想说的话说完就行。")


tool = {
    "name": "generate_comic",
    "description": (
        "画**连续多格小漫画**（同一角色、剧情连贯的一串图），或**一次出多张**"
        "符合某个需求的图。\n"
        "**只在用户明确要「漫画 / 连环画 / 多格 / 条漫 / 多图」，或要「N 张」"
        "（生成10张 / 来5张…）时用**。\n"
        "**单张图不要用这个**，用 generate_image。\n\n"
        "它会自己完成三件事：① 按 skills/storyboard-prompt 的规范把剧情写成"
        "「固定块（角色外观）+ 动态块（每格动作/场景/服饰/灯光）」；② 整批渲染；"
        "③ 画完一起发回来。**格数越多越慢**（10 格约 1 分钟）。\n\n"
        # 2026-10-16 用户拍板：「点名渠道必须是最高优先级的」。批量渠道只有
        # silver / anima 两条，用户点名的 nffa / krea2 系 / 三档 全都没有批量
        # 骨架——用这个工具等于替他丢掉点名的渠道（实录：私聊「krea2 鬼针草，
        # 横屏，和泉纱雾」→ 出 silver 漫画 10 格）。
        "**用户点名了具体渠道（nffa / krea2 鬼针草 / 三档 / qwen…）时别用这个"
        "工具**——点名渠道优先级最高，那种情况用 generate_image 出一张。\n\n"
        "参数：\n"
        "- brief（必填）：剧情 + 角色。把用户想要的故事、角色、场景写清楚，"
        "不用自己写分镜——分镜交给编剧。用户提到的角色名、作品名照抄进去。\n"
        "- panels：格数。用户报了数字就用用户的（「10个」「20格」）；"
        "没报就**不传**（silver 默认 10 / anima 默认 3）。硬上限 30。\n"
        "- style：渲染渠道。默认 silver；用户点名「anima多图 / anima 多图」时"
        "传 anima（anima 配方、默认 3 张）。**除此之外的画风名（qwen / jank 等）"
        "一律忽略**，别传、也别在回话里提。\n\n"
        "提交后系统会**直接**发一句回执到会话里，你**不要再复述张数、"
        "不要说「稍等 / 马上好」**——图会自己发出来。"
    ),
    "function": _generate_comic,
    "parameters": {
        "type": "object",
        "properties": {
            "brief": {
                "type": "string",
                "description": "剧情 + 角色。写清用户要的故事、角色（名字/作品名照抄）、"
                               "场景与要求。不用自己写分镜，编剧会补全每格画面。",
            },
            "panels": {
                "type": "integer",
                "description": "格数。用户报了数字（「10个」「20格」）就用用户的；"
                               "没报就**不传**（silver 默认 10 / anima 默认 3）。上限 30。",
            },
            "style": {
                "type": "string",
                "enum": ["silver", "anima"],
                "description": "渲染渠道。默认 silver；用户点名「anima多图」时传 anima。",
            },
        },
        "required": ["brief"],
    },
}
