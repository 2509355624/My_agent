"""连续小漫画工具：一句话剧情 → N 格连续分镜 → 逐格渲染发回会话。

触发形态（用户在 QQ 里说）：`大大怪，<画风>漫画，<数量>，<剧情>`
例：`大大怪，silver漫画，10个，原神的雷电将军在樱花树下和旅行者相遇`

工具只做三件事：
  ① 把剧情交给编剧（app/comic_story.write_story）写出「固定块 + 动态块 × N」；
  ② 起一个后台线程逐格渲染（app/comic_story.start），**不阻塞**适配层并发槽；
  ③ 直发一句回执，让模型闭嘴（别复述「稍等 / 马上好」，图会自己发出来）。

渲染渠道（画风）默认 silver；用户点名 qwen / jank / krea2 等就换。格数默认
10，用户报了数字就用用户的，硬上限 30。
"""

import logging

from app import comic_story, image_jobs
from app.tools.normal import generate_image as gi

log = logging.getLogger("tool.comic")

# 漫画可用的渲染渠道：都是「有文生图工作流、且适合连画 N 张」的。
# 刻意**不含**超清渠道（silver-hd / qwen-hd）——那些一张几十 MB、连画 10 张
# 又慢又占盘，用户真要点名「silver 超清漫画」再另说（现不在白名单里）。
_COMIC_SKILLS = ("silver", "jank", "qwen_image_v1", "image_gen_v1",
                 "krea2", "nffa")


def _norm_style(skill):
    """画风渠道收敛：认识就用，不认识（含空）一律回落默认 silver。"""
    s = (skill or "").strip()
    if s in _COMIC_SKILLS:
        return s
    if s.startswith("hd_3_"):          # 动漫三档（4 画风）
        return s
    return "silver"


def _generate_comic(brief, panels=None, skill=None):
    """brief = 剧情/角色描述；panels = 格数（默认 10）；skill = 画风渠道。"""
    # 生图总闸 + 单群闸 + 私聊额度闸（网页端不受限）。漫画复用同一套闸。
    gate = gi._qq_gate()
    if gate is not None:
        return gate

    from app import qq_api
    target, target_id = qq_api.current_context()
    if target is None:
        return "错误：连续漫画目前只在 QQ 里画，网页端用不了。"

    n = comic_story.clamp_panels(panels)
    style = _norm_style(skill)

    try:
        fixed, dyns = comic_story.write_story(brief, panels=n, style=style)
    except Exception as e:
        # 剧本没写出来 = 一张都没画，别让模型编「稍等」。说清楚，别重试。
        return ("错误：漫画剧本没写出来（%s），这一单一张都没画。"
                "直接告诉对方这次没成、别重试。" % e)

    comic_story.start(target, target_id, fixed, dyns, style)

    receipt = ("漫画已开工：%d 格，渠道 %s，正在逐格画，画好一张发一张。"
               % (len(dyns), style))
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
        "「固定块（角色外观）+ 动态块（每格动作/场景/服饰/灯光）」；② 逐格渲染；"
        "③ 画好一张发一张。**格数越多越慢**（10 格约 1 分钟）。\n\n"
        "参数：\n"
        "- brief（必填）：剧情 + 角色。把用户想要的故事、角色、场景写清楚，"
        "不用自己写分镜——分镜交给编剧。用户提到的角色名、作品名照抄进去。\n"
        "- panels：格数。用户报了数字就用用户的（「10个」「20格」）；"
        "没报就**不传**（默认 10）。硬上限 30。\n"
        "- skill：画风渠道，默认 silver。用户点名 qwen / jank / krea2 / nffa "
        "时才传对应的渠道名；只说「漫画」不传。\n\n"
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
            "skill": {
                "type": "string",
                "description": "画风渠道，默认 silver。用户点名 qwen / jank / krea2 / nffa "
                               "时才传（可用值：silver / jank / qwen_image_v1 / "
                               "image_gen_v1 / krea2 / nffa，或动漫三档 hd_3_<画风>）；"
                               "只说「漫画」不传。",
            },
        },
        "required": ["brief"],
    },
}
