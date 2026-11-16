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
    "id": "a_hai",
    "name": "阿海",
    "age": 26,
    "occupation": "城市海洋馆讲解员",
    "summary": "带过两千多场讲解，说话像随口聊天，喜欢用日常的东西打比方。",
    "background": (
        "阿海在城市海洋馆做讲解员，负责从大洋池一路走到水母厅的固定路线。"
        "他会记住每位游客上次走到哪里、问过什么，再用自己的话把同一件事讲出新的角度。"
        "说话自然、不背书，遇到游客说不清名字的时候，会先猜他想说的是哪一种。"
    ),
}


PLAY_MODES = [
    {
        "id": "guide",
        "label": "海洋馆问答",
        "description": "跟着讲解员走完整条路线，随时插话问展项、习性和冷知识。",
    },
]


_ROLE_RULES = """你是城市海洋馆的讲解员「阿海」，26 岁，带游客走一条固定的参观路线。
说话像自然的中文聊天：先回应游客这一句，再顺着往下讲，一两个自然段就够，不要背书式罗列。
你擅长用日常的东西打比方（比喻要具体，别用「仿佛」「宛如」这种书面词）。
记忆中出现的旧对话是你上次带这位游客时真的说过的内容：可以自然接着说「我们上次说到……」，
也可以直接用它回答问题；记忆里没有的事不要编，可以说「这条线我好像没跟你讲过」。
游客描述得含糊时（比如「那种会发光的鱼」），先按特征猜一种，说清是哪一种，再补一句区分点。
这是面向所有访客的公开展示：保持全年龄、非露骨内容，不写成人情节，不讨论内部版功能。
不要声称自己是人类，也不要声称拥有真实身体；可以演讲解动作，但不要混淆现实。
访客询问技术链路时，只根据随消息提供的展示轨迹客观说明，不编造模型、接口或系统细节。"""


_MODE_PROMPTS = {
    "guide": (
        "当前玩法是海洋馆参观。你和游客正走在馆内，路线从入口的大洋池一直往里。"
        "回答贴近展项本身：是什么、长什么样、怎么活、有什么反常识的地方。"
        "游客问路线上还没走到的区域时，可以指路，但不要假装已经一起看过。"
    ),
}


def roleplay_system(mode="guide"):
    """返回展示角色唯一的系统提示词；未知玩法回落默认，不接受自由文本。"""
    mode = mode if mode in _MODE_PROMPTS else "guide"
    return _ROLE_RULES + "\n" + _MODE_PROMPTS[mode]


# 所有场景都是不包含人物的原创环境图，减少预置工作流召回角色 LoRA 的机会。
# prompt 是最终提交给现有工作流的英文提示词；访客只能传 scene_id。
IMAGE_SCENES = [
    {
        "id": "rainy_bookstore",
        "title": "雨天书店",
        "brief": "阵雨中的街角书店，暖色灯光映在湿润石板路上。",
        "skill": "silver",
        "channel": "Silver · 现场生成",
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
        "skill": "silver",
        "channel": "Silver · 现场生成",
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
        "skill": "silver",
        "channel": "Silver · 现场生成",
        "prompt": (
            "an empty seaside tram stop at night, a vintage tram with warm yellow interior lights, "
            "dark blue ocean beyond the platform, moonlit clouds, reflections on clean metal rails, "
            "quiet cinematic anime scenery, crisp environmental details, balanced wide composition, "
            "no passengers, no people, no readable signs, no text, no logo"
        ),
    },
]


# 漫画区回放数据契约。成品图走展示专用素材（web/showcase/comic/），逐张过
# 公开审核后才允许把 image_url 从 None 改成素材地址；审核没过的格子保持
# pending，绝不借用内部漫画目录中的图片（2026-11-16 三格已生成并过审）。
COMIC_REPLAY = {
    "id": "last_light",
    "title": "最后一盏灯",
    "status": "ready",
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
                "1boy, 1girl, two survivors inside a dark convenience store during a power outage, "
                "checking shelves, flashlight beam cutting through darkness, shelves of canned food "
                "and bottles, backpacks, cinematic interior wide shot, detailed anime illustration, "
                "muted colors, no blood, no gore, no text, no logo"
            ),
            "negative": "3people, 3girls, 3boys, multiple boys, crowd, extra people",
            "image_url": "/api/showcase/comic/asset/panel_1",
            "asset_status": "ready",
        },
        {
            "id": "panel_2",
            "script": "他们把电池、饮用水和罐头分成两份，在地图上标出通往安全屋的路线。",
            "prompt": (
                "1boy, 1girl, two survivors inside a dark convenience store during a power outage, "
                "sorting bottled water, canned food and batteries on a checkout counter, marking a "
                "safe route on a paper map, flashlight beam, cooperative mood, cinematic interior "
                "wide shot, detailed anime illustration, muted colors, no blood, no gore, no text, no logo"
            ),
            "negative": "2girls, 3girls, 3people, extra people, crowd, child",
            "image_url": "/api/showcase/comic/asset/panel_2",
            "asset_status": "ready",
        },
        {
            "id": "panel_3",
            "script": "远处安全屋亮起一盏灯，两人带着物资穿过雨后街道，准备迎接下一页。",
            "prompt": (
                "1boy, 1girl, two survivors walking away down a rain-washed street at blue hour "
                "after the rain, toward a small safe house with one warm glowing window, reflections "
                "on wet asphalt, distant dark city skyline, hopeful mood, cinematic wide shot, "
                "detailed anime illustration, muted colors, no blood, no gore, no text, no logo"
            ),
            "negative": "3people, 3girls, 3boys, multiple boys, crowd, extra people",
            "image_url": "/api/showcase/comic/asset/panel_3",
            "asset_status": "ready",
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
