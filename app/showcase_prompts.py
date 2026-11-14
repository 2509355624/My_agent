# -*- coding: utf-8 -*-
"""公开展示页专用的干净素材契约。

这里不读取、改名或引用内部角色卡。访客只能选择本文件里的角色、玩法和
预置场景，不能通过请求体注入自定义生图提示词或切换内部功能。
"""

import copy
import os


PRODUCT = {
    "name": "大大怪 · 多智能体助手",
    "summary": "一个多智能体助手，把生图、角色扮演、长期记忆、漫画流水线串在一起。",
    "contact": {
        "email": "2509355624@qq.com",
        # 仓库地址由部署者配置；没配置时前端应隐藏该项，不展示伪造链接。
        "github": os.getenv("SHOWCASE_GITHUB_URL", "").strip(),
    },
}


ROLE_CARD = {
    "id": "lan_deng",
    "name": "岚灯",
    "age": 24,
    "occupation": "海滨城市公共档案馆管理员",
    "summary": "温和、细心，喜欢把杂乱的信息整理成清楚的小卡片。",
    "background": (
        "岚灯住在一座常有阵雨的海滨城市，白天整理地方志和老照片，"
        "傍晚会沿着有轨电车线路散步。她喜欢热茶、旧地图和雨天书店，"
        "说话自然简洁，会认真记住访客提过的喜好与约定。"
    ),
}


PLAY_MODES = [
    {
        "id": "daily",
        "label": "日常陪伴",
        "description": "围绕生活、阅读和城市散步展开的自由对话。",
    },
    {
        "id": "zombie_survival",
        "label": "小说模式 · 丧尸末日",
        "description": "在同一角色卡上切换末日生存世界观，以清理物资、寻找安全屋和互助求生为玩法。",
    },
]


_ROLE_RULES = """你是公开展示页中的原创角色「岚灯」，24 岁，是海滨城市公共档案馆管理员。
性格温和、细心、真诚，说话像自然的中文聊天，不堆砌形容词，不反复自我介绍。
你喜欢热茶、旧地图、雨天书店，擅长把复杂信息整理成容易理解的表达。
认真接续访客说过的话；有记忆材料时自然使用，没有时不要编造共同经历。
每次回复通常 1 到 3 个自然段，先回应当前这一拍，再视情况轻轻推进话题。
这是面向所有访客的公开展示：保持全年龄、非露骨的内容，不写成人情节，不讨论内部版功能。
不要声称自己是人类，也不要声称拥有真实身体或现实行动；可以角色扮演场景中的动作，但不要混淆现实。
访客询问技术链路时，只根据随消息提供的展示轨迹客观说明，不编造模型、接口或系统细节。"""


_MODE_PROMPTS = {
    "daily": "当前是日常陪伴玩法，场景从档案馆、书店与城市生活自然展开。",
    "zombie_survival": (
        "当前是小说模式：丧尸末日世界观。重点写物资清点、路线选择、安全屋和互助求生；"
        "保持紧张但不渲染血腥，不写酷刑或露骨伤害细节。访客与岚灯是互相扶持的同伴。"
    ),
}


def roleplay_system(mode="daily"):
    """返回展示角色唯一的系统提示词；未知玩法回落日常，不接受自由文本。"""
    mode = mode if mode in _MODE_PROMPTS else "daily"
    return _ROLE_RULES + "\n" + _MODE_PROMPTS[mode]


# 所有场景都是不包含人物的原创环境图，减少预置工作流召回角色 LoRA 的机会。
# prompt 是最终提交给现有工作流的英文提示词；访客只能传 scene_id。
IMAGE_SCENES = [
    {
        "id": "rainy_bookstore",
        "title": "雨天书店",
        "brief": "阵雨中的街角书店，暖色灯光映在湿润石板路上。",
        "skill": "moss-qunyou",
        "channel": "Moss 群友集 · 动漫插画",
        "prompt": (
            "a cozy corner bookstore on a rainy evening, warm amber light through large windows, "
            "wet stone pavement reflecting soft lights, shelves full of books visible inside, "
            "a small potted plant beside the door, gentle rain, atmospheric anime background art, "
            "detailed environment, cinematic composition, no people, no text, no logo"
        ),
    },
    {
        "id": "rooftop_garden",
        "title": "屋顶花园",
        "brief": "清晨的屋顶小花园，远处是刚醒来的海滨城市。",
        "skill": "moss-qunyou",
        "channel": "Moss 群友集 · 动漫插画",
        "prompt": (
            "a small rooftop garden at sunrise above a coastal city, wooden planters with herbs "
            "and white flowers, a round table with two empty chairs and a teapot, distant sea "
            "and tram lines, soft morning breeze, fresh green and warm gold palette, detailed "
            "anime background illustration, peaceful atmosphere, no people, no text, no logo"
        ),
    },
    {
        "id": "night_tram",
        "title": "夜间电车",
        "brief": "空旷的夜间电车站，蓝紫色海面与暖黄车厢灯相映。",
        "skill": "moss-qunyou",
        "channel": "Moss 群友集 · 动漫插画",
        "prompt": (
            "an empty seaside tram stop at night, a vintage tram with warm yellow interior lights, "
            "dark blue ocean beyond the platform, moonlit clouds, reflections on clean metal rails, "
            "quiet cinematic anime scenery, crisp environmental details, balanced wide composition, "
            "no passengers, no people, no readable signs, no text, no logo"
        ),
    },
]


# 漫画区首版提供可回放的数据契约。成品图必须后续放入展示专用素材并逐张审核，
# 在素材就绪前 image_url 保持 None，绝不能借用内部漫画目录中的图片。
COMIC_REPLAY = {
    "id": "last_light",
    "title": "最后一盏灯",
    "status": "assets_pending",
    "worldview": "丧尸末日 · 互助求生",
    "steps": [
        {"id": "script", "label": "分镜脚本"},
        {"id": "prompts", "label": "逐格提示词"},
        {"id": "images", "label": "成品图"},
    ],
    "panels": [
        {
            "id": "panel_1",
            "script": "傍晚，岚灯和同伴抵达断电的社区便利店，先检查入口与货架通道。",
            "prompt": (
                "two adult survivors cautiously entering a dark convenience store at dusk, "
                "checking aisles and exits, backpacks and flashlight, quiet tension, "
                "non-graphic post-apocalyptic storyboard, cinematic wide panel, no blood, no text"
            ),
            "image_url": None,
            "asset_status": "pending",
        },
        {
            "id": "panel_2",
            "script": "他们把电池、饮用水和罐头分成两份，在地图上标出通往安全屋的路线。",
            "prompt": (
                "two adult survivors organizing bottled water, canned food and batteries on a counter, "
                "marking a safe route on a paper map, flashlight illuminating their hands, "
                "cooperative mood, detailed comic panel, no blood, no readable text"
            ),
            "image_url": None,
            "asset_status": "pending",
        },
        {
            "id": "panel_3",
            "script": "远处安全屋亮起一盏灯，两人带着物资穿过雨后街道，准备迎接下一页。",
            "prompt": (
                "two adult survivors walking through a rain-washed street toward a small safe house "
                "with one warm window light, blue hour after rain, hopeful ending, "
                "cinematic comic panel, non-graphic, no blood, no text"
            ),
            "image_url": None,
            "asset_status": "pending",
        },
    ],
}


def image_scene(scene_id):
    """按白名单取场景；返回副本，调用方改不到契约本体。"""
    for scene in IMAGE_SCENES:
        if scene["id"] == scene_id:
            return copy.deepcopy(scene)
    return None


def public_config():
    """给页面首屏的公开配置；不包含内部角色卡路径或任何密钥。"""
    return {
        "product": copy.deepcopy(PRODUCT),
        "role_card": copy.deepcopy(ROLE_CARD),
        "play_modes": copy.deepcopy(PLAY_MODES),
        "image_scenes": [
            {k: copy.deepcopy(v) for k, v in scene.items() if k != "prompt"}
            for scene in IMAGE_SCENES
        ],
        "comic": copy.deepcopy(COMIC_REPLAY),
        "limits": {"chat_message_chars": 500, "history_messages": 12},
    }
