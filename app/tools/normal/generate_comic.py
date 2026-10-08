"""连续小漫画工具：一句话剧情 → N 格连续分镜 → 逐格渲染发回会话。

触发形态（用户在 QQ 里说）：`大大怪，<画风>漫画，<数量>，<剧情>`
例：`大大怪，silver漫画，10个，原神的雷电将军在樱花树下和旅行者相遇`

工具只做三件事：
  ① 把剧情交给编剧（app/comic_story.write_story）写出「固定块 + 动态块 × N」；
  ② 起一个后台线程整批渲染（app/comic_story.start），**不阻塞**适配层并发槽；
  ③ 直发一句回执，让模型闭嘴（别复述「稍等 / 马上好」，图会自己发出来）。

渲染渠道**只有一条**：批量小漫画工作流（`comic_story.COMIC_SKILL`，一次提交出
全部 N 张）。格数默认 10，用户报了数字就用用户的，硬上限 30。
"""

import logging

from app import comic_story, image_jobs
from app.tools.normal import generate_image as gi

log = logging.getLogger("tool.comic")


def _generate_comic(brief, panels=None):
    """brief = 剧情/角色描述；panels = 格数（默认 10）。渠道固定，不收参数。"""
    # 生图总闸 + 单群闸 + 私聊额度闸（网页端不受限）。漫画复用同一套闸。
    gate = gi._qq_gate()
    if gate is not None:
        return gate

    from app import qq_api
    target, target_id = qq_api.current_context()
    if target is None:
        return "错误：连续漫画目前只在 QQ 里画，网页端用不了。"

    n = comic_story.clamp_panels(panels)

    try:
        fixed, dyns = comic_story.write_story(brief, panels=n)
    except Exception as e:
        # 剧本没写出来 = 一张都没画，别让模型编「稍等」。说清楚，别重试。
        return ("错误：漫画剧本没写出来（%s），这一单一张都没画。"
                "直接告诉对方这次没成、别重试。" % e)

    comic_story.start(target, target_id, fixed, dyns)

    # 批量模式是**一次全给**，拿不到中间产物（见 comic_story.render 的注释），
    # 所以不能说「画好一张发一张」。
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
        "画**连续多格小漫画**（同一角色、剧情连贯的一串图）。\n"
        "**只在用户明确要「漫画 / 连环画 / 多格 / 条漫」时用**——"
        "用户说「<画风>漫画，<数量>，<剧情>」就是它（如"
        "「silver漫画，10个，原神的雷电将军在樱花树下和旅行者相遇」）。\n"
        "**单张图不要用这个**，用 generate_image。\n\n"
        "它会自己完成三件事：① 按 skills/storyboard-prompt 的规范把剧情写成"
        "「固定块（角色外观）+ 动态块（每格动作/场景/服饰/灯光）」；② 整批渲染；"
        "③ 画完一起发回来。**格数越多越慢**（10 格约 1 分钟）。\n\n"
        "参数：\n"
        "- brief（必填）：剧情 + 角色。把用户想要的故事、角色、场景写清楚，"
        "不用自己写分镜——分镜交给编剧。用户提到的角色名、作品名照抄进去。\n"
        "- panels：格数。用户报了数字就用用户的（「10个」「20格」）；"
        "没报就**不传**（默认 10）。硬上限 30。\n"
        "**没有画风参数**：漫画只有一条渲染路，用户点名的画风名（qwen / jank / "
        "silver 等）一律忽略，别传、也别在回话里提。\n\n"
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
                               "没报就**不传**（默认 10）。上限 30。",
            },
        },
        "required": ["brief"],
    },
}
