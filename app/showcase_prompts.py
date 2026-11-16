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
# 2026-11-16 重做：三格改由**漫画台**正规流程产出（自由视角 pov_mode=free、
# 角色一致性特化），不再是手工搓 prompt 的银色单图。
# 任务：comics/C20261116-01_末日余晖（silver_only 模式成品在 raw/）。
# 2026-11-16 晚二次重跑：用户拍板场景图渠道用 silver（稳定性最好），显式
# scene_skill=silver 走 comic/rerender 逐格重出；全局默认与群友锁仍是 anima-line，
# 没动。三图 808x1024 竖屏、逐张过 image_audit.check 公开审核才落展示目录，
# 审核没过保持 pending，绝不借用内部漫画目录原图。对外名仍叫《最后一盏灯》。
# image_url 带 ?v= 版本号做缓存击穿：素材原地覆盖后 URL 不变，浏览器/网关会拿
# 旧的缓存字节，所以每次换图必须同步改这里的版本号。
COMIC_REPLAY = {
    "id": "last_light",
    "title": "最后一盏灯",
    "status": "ready",
    "worldview": "丧尸末日 · 互助求生 · 漫画台自由视角",
    "source": "漫画台 · 自由视角（pov_mode=free）· silver 渠道 · 角色一致性特化 · 2026-11-16",
    "steps": [
        {"id": "script", "label": "分镜脚本"},
        {"id": "prompts", "label": "逐格提示词"},
        {"id": "images", "label": "成品图"},
    ],
    "panels": [
        {
            "id": "panel_1",
            "script": "傍晚，安娜和伊森抵达断电的社区便利店：她推门、他举起手电，两人警惕地检查入口与货架。",
            "prompt": (
                "1girl, 1boy, Anna, dark brown short hair in a ponytail, amber eyes, black hair tie, "
                "Ethan, black short hair with messy fringe, grey eyes, dusk, dim abandoned convenience "
                "store entrance, girl wearing a dark green waterproof jacket and black cargo pants with "
                "a black backpack, boy wearing a grey hooded jacket and dark blue cargo pants with a "
                "black backpack, girl pushing the glass door open with one hand holding a flashlight, "
                "boy stepping in behind her raising his flashlight, tense and alert expressions, "
                "cool dusk light fading into store darkness, wide shot, cinematic widescreen"
            ),
            "image_url": "/api/showcase/comic/asset/panel_1?v=20261116c",
            "asset_status": "ready",
        },
        {
            "id": "panel_2",
            "script": "收银台前，安娜把饮用水和罐头摆成一排，伊森俯身在纸地图上标出通往安全屋的路线。",
            "prompt": (
                "1girl, 1boy, Anna, dark brown ponytail, amber eyes, Ethan, black short hair, grey eyes, "
                "convenience store counter, girl in a dark green waterproof jacket and black cargo pants "
                "kneeling by the counter lining up bottled water and canned food, boy in a grey hooded "
                "jacket leaning over the counter marking a paper map with a pen, both with black "
                "backpacks, flashlight beams lighting the counter, focused and concentrated expressions, "
                "cool flashlight glow, medium shot, cinematic widescreen"
            ),
            "image_url": "/api/showcase/comic/asset/panel_2?v=20261116c",
            "asset_status": "ready",
        },
        {
            "id": "panel_3",
            "script": "雨后的蓝调时刻，两人提着物资走向远处亮着一扇暖窗的安全屋，伊森抬手指向前方。",
            "prompt": (
                "1girl, 1boy, Anna, dark brown ponytail, Ethan, black short hair, wet street after rain, "
                "blue hour, girl in a dark green waterproof jacket and black cargo pants walking beside "
                "boy in a grey hooded jacket, both carrying backpacks, girl carrying a supply bag, boy "
                "pointing toward a distant warm lit window, hopeful faint smile and relieved expression, "
                "wet asphalt reflecting blue sky and warm window light, cool blue hour light, full body "
                "shot from behind, cinematic widescreen"
            ),
            "image_url": "/api/showcase/comic/asset/panel_3?v=20261116c",
            "asset_status": "ready",
        },
    ],
}


# 自定义提示词上限（2026-11-16）：展示页公开展示，输入不能无限长。
# 跑图台私用上限是 10 万，这里只出一张图、还要在链路里原样展示，2000 字足够。
CUSTOM_PROMPT_MAX = 2000


def custom_image_scene(prompt):
    """用户自写提示词的场景：固定 silver 渠道、固定竖屏，其余链路同预置场景。"""
    text = str(prompt or "").strip()
    if not text or len(text) > CUSTOM_PROMPT_MAX:
        return None
    return {
        "id": "custom",
        "title": "自定义提示词",
        "brief": "你自己的提示词，原文送进工作流。",
        "skill": "silver",
        "channel": "Silver · 自定义提示词",
        "prompt": text,
        "custom": True,
    }


def ai_image_scene(prompt):
    """AI 扩写模式的场景：DeepSeek 补好的英文提示词，固定 silver / 竖屏。"""
    text = str(prompt or "").strip()
    if not text or len(text) > CUSTOM_PROMPT_MAX * 2:
        return None
    return {
        "id": "ai",
        "title": "AI 扩写",
        "brief": "你的需求经 DeepSeek 补充后的画面。",
        "skill": "silver",
        "channel": "DeepSeek → Silver · 固定竖屏",
        "prompt": text,
        "ai": True,
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
