"""QQ 角色扮演：人设卡 + 会话级扮演态 + 一轮纯文字对话。

跟生图机器人是**两条互不干扰的路**：

    默认态 → direct_gen / agent 循环，不带人设、能用工具
    扮演态 → 本模块：**不给模型任何工具**，一轮只做两件事

        拼极简头（角色人设 + 输出契约 + 环境）
          → 调 1 次 LLM → 正文发回 QQ，顺手写的那行画面拿去出一张快图（见下）

## 为什么是 1 次调用

参考的那套角色扮演系统一轮要 3 次（情感分析 → 对白 → 生图 tag）。但那三样里，
「人物此刻的情绪」本来就是模型写对白时脑子里已经有的东西，拆成三次只是当年的
写法；同时省掉两次完整输入的 token（那两次的输入跟第一次一样大）。

## 配图（2026-10-16 二次拍板）

同一天早些时候拍的是「扮演态不出图」——理由是配一张图要十几秒，而聊天是几秒
一句话的节奏，图赶不上对话。**当天下午换成了每轮配一张快图**：用户原话
「角色扮演模式的话，就用这个来快速生图，这个只要 2 到 3 秒钟而已，非常的快」，
指 `skills/_persona_fast`（480×720 / 3 步的 silver 漫画变体）。

于是这里的分工是：

    一次 LLM 调用 → 正文（立刻发）+ 「画面：」那一行（拿去出图）
                  ↓ 后台线程，不占这一轮
                出一张 ≈2 秒的图 → 发回同一个会话

三条边界：

1. **正文先走，图后到**。出图在后台线程里等，这一轮不阻塞——聊天节奏一点不变，
   图只是慢两三秒跟上来。
2. **画面由角色自己写**（用户拍板「角色回话时顺手写」）。这一行是输出契约的一部分
   （见 `_CONTRACT`），代码只负责把它从正文里剥出来，**不猜、不兜底**：没这一行就
   这一轮不出图，绝不拿上一轮或角色卡去凑一张对不上剧情的图。
3. **角色长相由卡上的外观字段钉死**（`appearancePrompt` / `outfitPrompt`）。模型
   只写「在做什么」，不写「长什么样」——不然每轮的脸都不一样。

## 作用域

进出都是**会话线级**的（私聊按 QQ 号、群按群号各一份），所以 A 群在演纱雾的
时候，B 群照旧是生图机器人。状态落 `state/persona_sessions.json`，**不进
settings.json**：那是用户在管理页手配的长期配置，扮演态是运行时状态，两者
混在一起会让管理页显示出一堆看不懂的键。
"""

import collections
import copy
import datetime
import functools
import glob
import json
import logging
import os
import random
import re
import shutil
import threading
import time

from app.features.persona import adult as adult_state
from app.features.persona import pregnancy
from app.features.persona import rag as persona_rag
from app.features.persona import items as _items
from app.features.persona import writing_style
from app.features.persona import writing_skills
from app.features.qq import trigger
from app.integrations import llm
from app.features.persona import affection
from app.config import (BASE_DIR, PERSONA_MODEL, PERSONA_MODEL_DEFAULT,
                        PERSONA_MODELS, PERSONA_PROVIDER,
                        persona_model_of,
                        QQ_AGENT_ID, QQ_BOT_NAME, LLM_JUDGE_TIMEOUT)

log = logging.getLogger("persona")

from app.features.persona.text_utils import MOODS
from app.features.persona.game_time import (
    PHASES, PHASE_ALIAS, _day_of, normalize_phase, phase_index,
)


# 请求级的模型选择（2026-11-14）。网页端玩家在下拉里挑了哪个模型，接口层
# `set_model_override()` 挂在本线程上，`_llm_call` 拼链头时用它顶掉默认值。
# 用 thread-local 而不是全局变量：Flask 每个请求一个线程，两个人同时聊不该
# 互相改对方的模型。
_MODEL_LOCAL = threading.local()


def set_model_override(model_id):
    """挂一个本线程的模型覆盖。传空/不在白名单 = 清掉，回默认值。"""
    got = persona_model_of(model_id)
    _MODEL_LOCAL.model = got["id"] if got else ""


def current_model():
    """本线程此刻真正会用哪个模型（白名单里的那条记录）。"""
    got = persona_model_of(getattr(_MODEL_LOCAL, "model", ""))
    if got:
        return got
    got = persona_model_of(PERSONA_MODEL_DEFAULT) or persona_model_of(PERSONA_MODEL)
    if got:
        return got
    return {"id": PERSONA_MODEL, "label": PERSONA_MODEL, "provider": PERSONA_PROVIDER}


def _llm_call(messages, **kw):
    """扮演链路的 LLM 调用：链头固定，玩家自选模型作为降级段。"""
    return llm_bridge.call(messages, current_model(), **kw)


Turn = collections.namedtuple(
    "Turn", "reply scene mood intensity state aff finish",
    defaults=("", 0, "", None, ""))

# 情绪词表（自由模式）。模型只能从这十个里挑，多一个字都不认——不限定的话
# 它会每轮自己发明一个词，代码就没法按强度做出图判断了。

# 出图门槛 / 服装判定关键词见 text_utils。

# 历史：最近 12 条（AI 和用户合计，即 6 轮），单条超 MSG_MAX_CHARS 截断。
#
# 2026-11-03：20 → 12。实测满 20 条时一轮输入 10,768 字，其中**历史里的助手
# 回复占 65%**（10 条 × 700 字）——正文写得越长，历史涨得越快，这是酒馆式
# 写法的固有代价。降到 12 条（6 轮）能省约 40% 输入。
#
# 记性由三层兜底：12 条滑动窗口 + 500 字小记（每 10 轮压一次）+ 状态里的
# 「她此刻的样子」。少了 8 条不会让人设或关系断裂，只是更早的细节会淡。
#
# 小记的触发轮数（NOTE_EVERY_TURNS=10）比窗口（6 轮）大，所以会出现「刚被
# 挤出窗口、还没进小记」的空档。影响可控——那段内容本来也要被压成摘要了。
HISTORY_LIMIT = 12

# 前缀缓存的滞回窗口（2026-11-10）：送模型的历史**逐轮只追加，不滑动**。
#
# DeepSeek 按「逐字节相同的前缀」分价计费：命中 0.02/token、未命中 1.0/token，
# 差 50 倍。原来每轮都取 `[-12:]`，聊过 6 轮之后每轮都会踢掉最老一条，整段
# 历史跟着错位一位，后面的前缀全废 —— 账单里主回复的 hit 常年卡死在 15,104
# （正好是 system 头的长度），miss 却有 8,000~9,000，那部分就是每轮重付的历史。
#
# 改成滞回：窗口从 12 条一路追加到 30 条（15 轮）期间**起点不动**，每轮只往
# 尾巴加两条，前缀因而整段命中；超过 30 条才一次性砍回 12 条，那一轮断一次，
# 之后重新开始累积。约 10 轮里 9 轮吃到长前缀，命中稳过 2 万。
#
# 落盘文件就当这个窗口用：网页回看读的是全量归档（`load_archive`），不依赖它；
# 全量原文也一直在 `_append_archive` 里，砍窗口不丢历史。
HISTORY_CACHE_HIGH = 30

# 单条消息的截断上限（2026-11-02：200 → 900 → 2026-11-06：2400
# → 2026-11-07：2600）。契约把正文目标定到 600~2000 字之后，2400 对
# 「2000 字正文 + 三行元数据 + 分段标签」只剩不到 300 字余量，写满一轮
# 就会被截掉尾巴。2600 让一整段完整场景原样进历史。
# ⚠️ 这是**网页端 / 扮演历史**的宽度，跟 QQ 的 QQ_REPLY_MAX_CHARS=700
# 没关系：那个只在 qq_api.split_message 里把长回复切成几条发，内容不丢。
# 2026-11-07 用户明确「QQ 那边不动，网页端放宽」。
# 注意只放宽**单条**：HISTORY_LIMIT 仍是 12 条，用户要的是把内容写厚，
# 不是把上下文窗口撑大。
MSG_MAX_CHARS = 2600

# 网页回看历史（2026-11-04 用户要求「能回到看从开始到现在」）：一页多少条。
# 24 条 = 12 轮，和模型窗口同量级——首屏不会一次塞几百条，再往上滚由前端
# 要更早的一页。这只是**展示**分页，不进模型、不改上下文预算。
ARCHIVE_PAGE = 24

# 采样温度（2026-11-03）。
#
# `llm.call_llm()` 原来**根本不支持这个参数**，温度一直走服务端默认（一般
# 0.7）。那个温度写日常对话够用，写亲密场景就偏保守——句子四平八稳、比喻
# 都是熟词，没有「野性」。
#
# 1.05 是个折中：再高（1.2+）开始出现语义漂移和错别字，再低回到套路。
# 想更放得开就往上调，代价是偶发的逻辑跳跃。
TEMPERATURE = 1.05

# 小记：每 NOTE_EVERY_TURNS 轮在后台压一次，压到 NOTE_MAX_CHARS 字以内。
# 不做每轮压缩——那等于把「1 次调用」省下来的钱又还回去。
#
# 2026-11-03：10 → 5。**必须小于历史的轮数**（HISTORY_LIMIT // 2 = 6）。
# 原来设 10 而窗口是 10 轮，正好接得上；窗口降到 6 轮之后，如果这里还是 10，
# 第 7~10 轮会「掉出窗口、又还没被压缩」，永久丢失。压到 5 轮一次更保险。
#
# 2026-11-03 晚：500 → 1000 字（用户要求「尽可能保留相关的记忆」）。
# 代价算过：
#     小记 500   → 一轮总输入约 8928 字 ≈ 5800 token
#     小记 1000  → 一轮总输入约 9428 字 ≈ 6130 token     （只多 325）
#     小记 1500  → 一轮总输入约 9928 字 ≈ 6450 token
# 1000 字这一档性价比最好：记忆翻倍，token 只涨 5%。
# 对照：今天生图链路（含编剧）实测单次约 3000~5000 token，
# 所以「扮演一轮 ≈ 生图一次」这个用户定的目标是达成的。
NOTE_MAX_CHARS = 1000
NOTE_EVERY_TURNS = 5

# 提示词里报给模型的字数，比硬上限留一成余量。
#
# 2026-11-04 用户报的 bug：他做的早餐，AI 说成是自己做的，而且「回忆」看着
# 一直不更新。查下来是 `note[:1000]` 硬切——小记按时间顺序写，**最新的事在
# 末尾**，只要模型写超一点，被切掉的永远是刚发生的那段。日志里这条线连续
# 7 次压缩全是「正好 1000 字」，最后一次切在「第二天」三个字后面，早餐整段
# 没进记忆；再叠上 12 条窗口已经把「我准备了早餐」挤出去，模型手里再没有
# 任何反证，就顺着「谢谢纱雾」演成「谢谢我做的饭」。
NOTE_SOFT = 900

# 「故事到现在」发展线的字数上限（2026-11-07）。
#
# 用户报「AI 不知道前面几轮聊的事情怎么发展下来的」：短期窗口只有 12 条，
# 账本每 5 轮才压一次，中间那几轮两边都没有。`story_line()` 把
# **时间/地点 → 已经发生 → 还没完成** 按因果顺序排成一条短块，贴在近因位。
# 只搬已经存在的事实（gameday + 账本六小节），不调模型、不加玩家操作。
# 320 字 ≈ 一份短账本的 1/3，够把「现在在哪一段、手上还欠着什么」说清。
# 2026-11-10 放宽到 600：用户报「跨天记不住细节」，320 字把账本每节末尾
# 2~3 条压成骨架，具体物件与事件被裁掉。多出来的字落在末尾 miss 区，
# 每轮约多 200 token，比把细节丢了再让模型编一遍便宜。

# 扮演态的静默窗口（秒）：这之内的连发算同一条。用户 2026-10-16 定的 10 秒。
# 群聊默认那 1.5 秒是给「连发三句问同一件事」用的；聊天不一样——人打字是
# 断续的，1.5 秒会把一句话切成两轮，回两遍。
DEBOUNCE_SECONDS = 10.0

from app.features.persona.prompt_blocks import (
    _AROUSAL_LAYERS, _CORRUPT_LAYER, _FORCED_LAYER, _INTIMATE_GUIDE,
    _ORGASM_LAYERS, _TIME_LAYERS, _pick_level, _combo_hits, body_state_block,
    contact_block, intimate_guide, normalize_contact,
)

from app.features.persona.contract import (
    _CONTRACT, _CONTRACT_NO_INTIMATE, _CONTRACT_TAIL, _GROUP_NOTE, _NOTE_PROMPT,
)

from app.features.persona import state_store
from app.features.persona import history_store
from app.features.persona.lines import (
    SLOT_MAX, _card_key_slot, _persona_key, _slot_suffix,
    card_key, card_slot_of,
)

from app.features.persona.game_text import (
    _DAY_TURNS_SOFT, _EVENT_POOL, _gameday, _gameday_block,
    _plan_fact, _roll_event,
)

from app.features.persona.manual_input import (
    _MANUAL_KEYS, _manual_block, _manual_display, _manual_norm,
    _manual_segments,
)

from app.features.persona.json_utils import _close_json, _parse_json_loose
from app.features.persona import comic_brief as comic_brief_text
from app.features.persona.prompts import (
    _AUTO_CONFESS_TASK, _FINISH_ACT_TASK, _FINISH_CUSTOM_HINT,
    _FINISH_END_TASK, _FINISH_LAST_TASK, _FINISH_MORNING_TASK,
    _GUARD_TEXT, _INTENT_PROMPT, _OPENING_PROMPT, _PHASE_TASK,
    _SCENE_DUTY, _SCORE_PROMPT, _SCORE_PROMPT_V2, _SKIP_TASK,
    _SKIP_TASK_DOING, _SKIP_TASK_MULTI, _THIS_TURN_ECHO,
)

from app.features.persona.user_block import _visible_user_text, render_user_block
from app.features.persona import head_builder
from app.features.persona import llm_bridge
from app.features.persona.llm_bridge import REPLY_MAX_TOKENS
from app.features.persona.head_builder import (
    _env_note, _guard_block, _note_block, _state_block,
)
from app.features.persona.history_render import (
    pronoun_to_name as _pronoun_to_name, render_hist as _render_hist,
)
from app.features.persona import roster
from app.features.persona.prompt_facts import (
    FACT_MAX, FACT_TEXT_MAX, _FACTS_GUIDE, _facts_block, _outfit_block,
    _parse_facts, _place_block,
)
from app.features.persona.story import (
    STORY_MAX_CHARS, _clip_items, _ledger_section,
    _shares_phrase, story_line,
)




# 命令词。都带 `#` 前缀，且要求**整条消息只有这一个词**，免得误伤
# 「#角色扮演能不能画图」这种正常问句。
LIST_WORDS = ("角色扮演", "人设列表")
EXIT_WORDS = ("出戏",)
# `#重置` / `#重开` / `#从头开始`：把这条线的记忆清干净、**保留当前角色**重新开始。
#
# 2026-11-05 用户实测反馈：发「清空」没有任何确认就把全部记忆删了，不可接受。
# 于是把这个词从表里拿掉，并且**这些词也不再直接删**——统一走 `wipe_request`
# 的两步确认（亮红灯 → 引用那条警告 → 回确认句），和「清空存档」一个闸门。
CLEAR_WORDS = ("重置", "重开", "从头开始")

# 攻略模式（2026-11-03）：好感度数值层。判据和上面一样——**整条消息**才认。
# 「攻略模式」开（有封存就解冻）、「自由模式」关（数值封存不清零）、
# 「好感」查档案。2026-11-04 起不再有「只清数值、记忆留着」的中间档：
# 要么接着玩（自由模式封存），要么整份删掉（清空存档，要二次确认）。
RAID_WORDS = ("攻略模式",)
FREE_WORDS = ("自由模式",)
# 「睡到明天」（2026-11-04）：推进游戏内一天，她先开口。词要挑**聊天里
# 不会自然出现**的——「明天见」这种就不能进表，整条消息匹配也会误触。
SKIP_WORDS = ("睡到明天", "跳过今天")
# 「回退 / 撤回」（2026-11-04，2026-11-05 加「撤回」）：撤掉上一轮——她的回复
# 从聊天里撤回、历史里删掉、好感/游戏日/心情退回这轮之前。**真正的闸门不在词
# 上**：单发这两个字不触发（聊天里太常见），必须**引用机器人上一条扮演回复**
# 再发（判据在 `qq_bot` 侧：引用的 message_id 得对得上它记的上一轮）。
ROLLBACK_WORDS = ("回退", "撤回")
AFF_WORDS = ("好感", "好感度")
# 状态面板（2026-11-04 深夜）：用户要的「一列一列看数值和对应状态」。
# 和「好感」的区别：好感是两页档案（数值 + 最近变化），状态是**一张全表**——
# 身体进度七级、感情七档、冲动五档、高潮五档，每行「当前值/门槛」。
# 玩家要的不是回顾，是「我还差多少」。


STATUS_WORDS = ("状态", "进度", "面板")
# 「清空存档」（2026-11-04）：原来叫「重新开始」，只清数值、聊天记忆留着，
# 玩家删完发现她还记得上周聊的，觉得「没惩罚」。现在改成真删，并且要二次确认。
# 词表 `WIPE_WORDS` / 确认句 `WIPE_CONFIRM` 和 pending 一起放在 `wipe_request` 那边。
# 「再给我一次机会」（2026-11-04）：结局之后的明面出口，每人 3 次。
# 效果和「切自由模式再切回来」一模一样（结局解除、好感往线内挪一格），
# 只是它留在攻略模式里，而且会扣次数。
MERCY_WORDS = ("再给我一次机会", "给我一次机会")
# 成就（2026-11-04）：只认确定性事件，见 `app/achievements.py`。
# 单独一个词是为了不和「好感」混——好感是「现在到哪」，
# 成就是「做成过什么」。
ACH_WORDS = ("成就", "勋章")

_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

# 扮演态专用的配图渠道。
#
# 2026-11-02 换成 `silver`（用户定）：默认生图渠道，两段采样（12 步 + 8 步）
# 能把提示词里的服装描述落实下来。之前的 `_persona_fast` 是单段 3 步的
# BatchPromptImageGenerator，同样 6 个 LoRA、同一个底模，但 3 步只抓最强的主语
# （`1girl, silver hair, solo`），「浅灰色宽松卫衣」这种后段弱语义直接丢，
# 实测画成了裸体。
#
# ⚠️ 2026-11-03：扮演态**不再自动出图**（用户拍板）。配图改由用户主动说
# 「出图」触发。这个常量留给 `draw()` 的兼容路径，正常流程不再走它。
FAST_SKILL = "silver"

# 「出图」的渠道（2026-11-04 用户拍板）：moss 五格漫画剧情差、出图慢，
# 换成 anima-line 单张横屏 CG——画面行本来就是英文定稿，零额外 LLM 调用；
# 角色 LoRA 自动装填（`app/char_loras.py`）挂在这条渠道上，IP 角色自动吃加成。
#
# 2026-11-09 用户拍板换成 `anima-2s`，当天下一步又拍板换回 `silver`：
# **角色扮演的默认出图渠道，横屏竖屏都是它**。anima-line / anima-2s 没下架，
# 玩家点名照跑。换渠道要同步 `skills/<渠道>/`、QQ 白名单、
# `char_loras._LORA_SKILLS_ANIMA`（角色 LoRA 只挂在 anima 那套上）。


SHOT_SKILL = "silver"

# 扮演配图的输出目录（2026-11-07 用户点名）：落到
# `output/角色扮演生图/<渠道>/`，和 `web/生图页/` 分开。
# 以前不传 `out_dir` 时按 target 推断——网页线会落到 `web/生图页/`，
# 和玩家手动出的图混在一起，谁也分不清哪张是聊天里生成的。
PLAY_OUT_DIR = "角色扮演生图"


# ─── 用户（男主）的固定外观 ────────────────────────────────
#
# 2026-11-03 用户拍板：出图时要有统一的男主，代表用户本人。没有这个的话
# 六格漫画里的男方每格长得都不一样（moss 的 cast 由编剧自己写，不给他
# 固定的串，他会每页临时编一个）。
#
# 2026-11-08：这串**只给漫画 brief 用**（见 comic_brief）。角色扮演的单张 CG
# 已经改成 POV 视角，画面里只有她 + 你的一只手，`draw()` / `draw_shot()` 不拼
# 这串——别把它接进单张路径，那会把男主的脸画出来。
# 写成中性、可复用的描述：成年男性、短发、日常深色上衣。**不要写具体长相**
# ——那是用户自己的脸，读者要能代入。
MALE_PROTAGONIST = (
    "an adult man in his late twenties, short black hair, plain dark shirt, "
    "average build, natural proportions"
)

# 「出图」触发词。用户说这些就出一张 `SHOT_SKILL`（silver）单张 CG
# （QQ 固定竖屏，网页端由弹窗选横/竖——横屏是给电脑背景用的，见 `draw_shot`）。
#
# 判据要严：`出图` 两个字太常见（「我刚才出图了」「出图失败」都是正常聊天），
# 所以**只在扮演态里**、且整条消息就是这个意思时才触发。见 `is_shot_request`。
SHOT_WORDS = ("出图", "画一张", "画一下", "来张图", "出张图", "画个图")


def is_shot_request(text):
    """这条消息是不是在要求「按当前场景出图」。

    只认**整条消息**是触发词（可带 # 前缀、可带「一下」「吧」这类语气词），
    不做子串匹配——扮演态里的「你刚才出图了？」是在聊天，不是在下命令。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if t.startswith("#"):
        t = t[1:].strip()
    if t in SHOT_WORDS:
        return True
    # 去掉结尾的语气词再比。**注意顺序**：先比原串再剥——「画一张」本身就在
    # 词表里，先剥「一张」会剥成「画」，反而认不出（实测踩过）。
    for tail in ("吧", "啊", "呀", "呢", "一下", "一张", "！", "!", "。", "."):
        if t.endswith(tail):
            t = t[: -len(tail)].strip()
            if t in SHOT_WORDS:
                return True
    return False


def _shot_parts(session_key, msg_index=None):
    """「出图」要用的素材：`(用户原话, 她的正文, 画面行, 状态)`。

    `msg_index=None` → 最后一轮（扫窗口）；数字 → **归档第 N 行**的 assistant
    （必须是 assistant 行，行号只增不删，不会随窗口裁剪漂移）。
    `comic_brief`（moss 漫画 brief）和 `shot_scene`（单张 CG）共用这套提取，
    保证两个出图路径画的是同一轮。
    """
    session_key = resolve_line(session_key)
    ent = _entry(session_key) or {}

    last_user = ""
    last_ai = ""
    scene = ""                  # 「画面：」那一行——本轮画面的定稿
    state = str(ent.get("state") or "").strip()

    if msg_index is None:
        hist = [m for m in trim(_load_hist(session_key))
                if m.get("role") in ("user", "assistant")]
        for m in reversed(hist):
            if m["role"] == "assistant":
                body, sc, _mo, _it, _st = split_turn(m["content"] or "")
                if not last_ai:
                    last_ai = body.strip()
                    scene = (sc or "").strip()
            else:
                body = str(m["content"] or "").strip()
                if not last_user:
                    last_user = body
            if last_user and last_ai:
                break
    else:
        # 按**归档下标**定位（2026-11-04）。以前这里是「窗口里第几条 assistant」，
        # 而窗口每次落盘都会裁，聊一轮整体前移一位——网页点旧气泡出图时传的
        # 是当时的号，压缩后就指到别人那条上。归档只增不删，行号才是稳定锚。
        try:
            n = int(msg_index)
        except (TypeError, ValueError):
            n = -1
        rows = load_archive(session_key)
        if 0 <= n < len(rows) and rows[n].get("role") == "assistant":
            last_ai, sc, _mo, _it, st = split_turn(rows[n].get("content") or "")
            last_ai = last_ai.strip()
            scene = (sc or "").strip()          # 用**那一轮**的画面，不是最后一轮
            if st.strip():
                state = st.strip()      # 用**那一轮**的状态，不是此刻的
            # 往前找最近的一条用户消息
            for j in range(n - 1, -1, -1):
                if rows[j].get("role") == "user":
                    last_user = str(rows[j].get("content") or "").strip()
                    break
    return last_user, last_ai, scene, state


def shot_scene(session_key, msg_index=None):
    """「出图」这一轮的「画面：」行（英文定稿）。没有就返回空串。

    没有画面行就**直接提示没得画**（2026-11-04 用户拍板），不做 LLM 兜底
    翻译——正常轮次的输出契约保证每轮都有画面行，空是极少数边缘情况。
    """
    return _shot_parts(session_key, msg_index)[2]


def shot_outfit(session_key, msg_index=None):
    """「出图」这一轮的「服饰：」行（当前穿着的英文定稿）。

    优先取**那一轮**归档/窗口里的原文：点旧气泡出图时，画的是当时的穿着，
    不是此刻的。老线没有这一行（功能上线前的归档），回落到当前固化的
    `ent["outfit"]`；再没有就返回空串，`build_prompt` 走老拼法。

    和 `_shot_parts` 分开，是为了不改它那个四元组返回（漫画 brief 也按它拆包）。
    """
    key = resolve_line(session_key)
    ent = _entry(key) or {}
    cur = str(ent.get("outfit") or "").strip()
    if msg_index is None:
        rows = [m for m in trim(_load_hist(key))
                if m.get("role") == "assistant"]
        if rows:
            return split_outfit(str(rows[-1].get("content") or "")) or cur
    else:
        try:
            n = int(msg_index)
        except (TypeError, ValueError):
            n = -1
        rows = load_archive(key)
        if 0 <= n < len(rows) and rows[n].get("role") == "assistant":
            return split_outfit(str(rows[n].get("content") or "")) or cur
    return cur


def draw_shot(card, session_key, msg_index=None, reply_to="", on_submit=None,
              landscape=False):
    """「出图」→ `SHOT_SKILL`（silver）单张 CG，默认**竖屏**。

    2026-11-09 用户拍板：默认渠道从 anima-2s 换回 `silver`，**横屏竖屏同一条**。
    `landscape=True`（网页背景用）走 `image_jobs.landscape_workflow()` 把尺寸
    节点宽高对调，不是新渠道，采样/LoRA 全都不动。

    横屏唯一的实测坑：提示词里写「两个人并排」会被画成两个女孩（
    `two people standing side by side` → 双胞胎式同款浴衣）。要「一起看」的
    意思，靠 POV 那只手表达，别在提示词里数人数。

    返回 `(文件名, note)`：note 非空 = 没画出来及**真实原因**（2026-11-08：
    排队满 / 没在线 / 生成超时 / 跑完没图，各自说各自的）。**阻塞**几十秒，
    调用方放后台线程。和 moss 漫画共用同一套画面提取（`_shot_parts`），
    但不经编剧——画面行本来就是给生图写的英文定稿，直接拼角色外观串用。

    引用内容（`extra`）在单张路径里**不参与**：画面行是权威，冲突时以它为准；
    没有画面行时按拍板直接拒绝，不拿中文引用原文硬喂给 CLIP。
    """
    scene = shot_scene(session_key, msg_index)
    if not scene:
        return "", "这一轮没有可画的画面"
    # 排查用（2026-11-05）：线上反馈「出图还是六格漫画」。这条日志把
    # **实际用的渠道和画幅**打出来，一眼就能分辨是代码没生效（进程跑着旧
    # 版本 → 没有这行）还是走了别的入口（这行有但渠道不是 SHOT_SKILL）。
    outfit = shot_outfit(session_key, msg_index)
    log.info("扮演出图：渠道=%s 画幅=%s 行号=%s 画面=%s 服饰=%s",
             SHOT_SKILL, "横屏" if landscape else "竖屏", msg_index,
             scene[:60], outfit[:60])
    name = draw(scene, card, session_key, reply_to=reply_to,
                skill=SHOT_SKILL, landscape=bool(landscape),
                outfit=outfit, on_submit=on_submit)
    if not name:
        # 真实原因在 thread-local 上（draw 的返回值十几个调用方都当字符串用，
        # 不改签名）。拿不到才退回老的那句笼统文案。
        return "", (getattr(_DRAW_ERR, "reason", "")
                    or "没画出来（排队满 / ComfyUI 没在线 / 被审核拦下）")
    return name, ""


def comic_brief(card, session_key, extra="", msg_index=None):
    """把「用户点名要画的 + 她此刻 + 状态 + 人物」拼成 moss 漫画的 brief。"""
    session_key = resolve_line(session_key)
    name = char_name(card, "女主角", key=session_key)
    look = _fixed_block(card)
    last_user, last_ai, scene, state = _shot_parts(session_key, msg_index)
    return comic_brief_text.build(
        name, look, last_user, last_ai, scene, state, extra, MALE_PROTAGONIST)


# 画面 / 服饰行正则见 text_utils。


# ─── 卡库 ────────────────────────────────────────────

from app.features.persona.cards_store import (
    SaveCardsRejected, _STATE_BEHAVIOR_ALIAS, _STATE_BEHAVIOR_KEYS, _as_int,
    _cards_path, _digits_only, _fresh_id, _level_names, _norm_card,
    _norm_state_behavior, _state_behavior_text, _write_cards_raw,
    is_builtin_card, load_cards, save_cards, update_card_fields,
    user_card_owner,
)

from app.features.persona.ledger_text import (
    _LEDGER_SECTIONS, _PRONOUN_HE, _clip_ledger, _clip_note, _cut_sentence,
    _ledger_fix_pronouns, _ledger_ok,
)




def get_card(card_id):
    """按 id 取卡。`u<qq>_` 前缀的走那个人的自建卡文件（2026-11-09）。

    这里认前缀而不是「先查官方库再查本人库」，是为了让**所有**按 id 取卡的调用点
    （`active_card` / `_erase` / 管理页名字 / 出图取卡面）不改一行就能演自建卡——
    线键里存的就是 id，归属从 id 本身就能读出来，不需要额外传「现在是谁」。
    """
    cid = str(card_id or "").strip()
    if not cid:
        return None
    owner = user_card_owner(cid)
    if owner:
        return get_user_card(owner, cid)
    for c in load_cards():
        if c["id"] == cid:
            return c
    return None


def find_card(word, qq=""):
    """按卡名找卡；找不到再按 id 找。**精确匹配**（忽略首尾空白与大小写）。

    不做模糊匹配是有意的：`#` 开头的消息认不出卡就**当普通消息放行**（见
    `handle_command`），所以认错比认不出更糟——模糊匹配会让「#银发少女怎么画」
    这种正常问句被吃成命令。

    `qq`（2026-11-09 玩家自建角色）：传了就在这个人的自建卡里也找一遍，
    **默认不传 = 只看官方库**。QQ 那边全程不传，所以别人的自建卡既不进群的名单、
    也不会被 `#命令` 认出来；只有网页端知道自己是谁，才传这个参数。
    """
    key = str(word or "").strip()
    if not key:
        return None
    low = key.lower()
    cards = load_cards()
    for c in cards:
        if c["name"].lower() == low:
            return c
    # 自定义名字（charName）也认：玩家记住的多半是「苏暖」，不是卡名「青梅竹马」。
    # 仍然精确匹配，不做模糊，理由同上。
    for c in cards:
        if str(c.get("charName") or "").strip().lower() == low:
            return c
    owner = _digits_only(qq)
    if owner:
        mine = load_user_cards(owner)
        for c in mine:
            if c["name"].lower() == low:
                return c
            if str(c.get("charName") or "").strip().lower() == low:
                return c
    for c in cards:
        if c["id"].lower() == low:
            return c
    card_owner = user_card_owner(key)
    if card_owner:                # 自建卡的 id 不在官方库里
        # 传了 qq（玩家自己找卡）→ **只认自己的**：别人的卡 id 猜到你头上也当没有。
        # 不传 qq 的是查看台 `_card_name()` 那类只读展示，站主必须能按 id 解析出
        # 是谁的卡，所以那一支放行。
        if owner and card_owner != owner:
            return None
        return get_card(key)
    return None


# ─── 玩家自建卡（2026-11-09 用户要求）───────────────────
#
# 口径：**只本人可见**、**只在网页端做**、**按 QQ 号一人一份文件**
# ——`state/persona_user_cards/<qq>.json`。
#
# 为什么不塞进 `agents/qq/characters.json`：那份库是 `save_cards()` **整表覆盖**
# 写的，出过事故（提交的列表少半截 → 25 张卡静默变 12 张，见 `SaveCardsRejected`）。
# 玩家的写入一旦走同一份文件，等于任何人都可能覆盖站主库；分文件之后两边互不参与，
# 删了某个玩家的文件也伤不到官方卡。
#
# id 规则 `u<qq>_<slug>`：归属直接写进 id。`get_card()` 拿到 id 就知道去谁的文件找，
# 于是整条引擎链——存档槽、状态数值、她的名字、出图、怀孕记账——**不用加任何
# 「当前是谁」的上下文**就能演自建卡。
#
# QQ 侧刻意不认自建卡：`load_cards()` 仍旧只有官方库，`find_card()` 要找自建卡
# 必须显式传 `qq`。别人的卡不会出现在你的名单里，反过来也一样。

USER_CARD_MAX = 20                 # 每人最多几张
USER_CARD_NAME_MAX = 24            # 卡名长度（列表和 #命令都要用它）

# 玩家能写的字段 + 各自的长度上限。**没列在这里的字段一律不收**：
# `char_lora`（角色 LoRA 是站主的资产库 key，玩家乱填会把出图搞坏）、
# `referenceImage`（服务器上的图片路径）、`hidden`（下架开关）、`builtin`
# 都只对站主开放。`systemPrompt` 是玩家写给模型的演出指令，长度必须钉死——
# 它每轮都在 system 头上，一张卡写两万字会把整条链的预算吃掉。
USER_FIELD_MAX = {
    "name": USER_CARD_NAME_MAX,
    "charName": 20,
    "description": 3000,
    "personality": 1500,
    "systemPrompt": 4000,
    "exampleDialogue": 3000,
    "appearancePrompt": 800,
    "outfitPrompt": 400,
    "room": 1500,
    "opening": 1200,
    "avatar": 500,
}

_INT_FIELD_MAX = {
    # 玩家能写的起点只有 v2 那四条轴（2026-11-09 用户拍板）。
    # `start_level` / `start_arousal` 是 v1 老标尺的字段，新线建成 v2 之后
    # 它们不参与任何判定——留在表单里就是让人填一个空壳，所以**不在白名单**：
    # 手搓请求塞进来也会被 `_clean_user_fields` 丢掉，落回 `_norm_card` 的默认。
    "start_value": (-50, 100),          # 好感：v2 满分 100，50/75 两档要在一起
    "start_confess": (0, 50),           # 表白值：50 = 该她开口了
    "start_yandere": (0, 100),          # 病娇值：45 / 75 各出一个事件
    "start_arousal_tier": (0, 4),       # 冲动是 0~4 档，不是数值
}


def user_cards_path(qq):
    """这个人的自建卡文件；QQ 号不合法（非纯数字）→ None。

    目录跟着 `_STATE_PATH` 走（不是 `state_path()`）：生产上两者都落在
    `state/`，但测试用例是每个用例 patch 一份 `_STATE_PATH`，用它才能做到
    「这个用例建的卡不会串到下一个用例」——`state_path()` 是**整进程共享**
    一个临时目录，20 张上限和重名判断会被前面的用例污染。
    """
    s = _digits_only(qq)
    if not s:
        return None
    return os.path.join(os.path.dirname(_STATE_PATH),
                        "persona_user_cards", s + ".json")


def load_user_cards(qq):
    """这个人的自建卡。文件不在 / 坏了 / 不是列表 → 空列表（同 `load_cards()`）。

    `owner` 一律按**文件名**覆写：卡里自己写谁都数，文件在谁名下就是谁的——
    否则往别人的文件里塞一张 owner=自己 的卡就能冒充。
    """
    path = user_cards_path(qq)
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        log.exception("读自建卡失败 %s", path)
        return []
    if not isinstance(data, list):
        log.warning("自建卡文件不是列表，忽略：%s", path)
        return []
    owner = _digits_only(qq)
    cards, seen = [], set()
    for c in data:
        if not isinstance(c, dict) or not str(c.get("name") or "").strip():
            continue
        card = _norm_card(c)
        card["owner"] = owner
        if card["id"] in seen:
            card["id"] = _fresh_id(seen)
        seen.add(card["id"])
        cards.append(card)
    return cards


def _write_user_cards(qq, cards):
    path = user_cards_path(qq)
    if not path:
        return False
    from app.core.agents import _atomic_write_text
    owner = _digits_only(qq)
    out = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        card = _norm_card(c)
        if not card["name"]:
            continue
        card["owner"] = owner
        out.append(card)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _atomic_write_text(path, json.dumps(out, ensure_ascii=False, indent=2))
    return True


def get_user_card(qq, cid):
    cid = str(cid or "").strip()
    for c in load_user_cards(qq):
        if c["id"] == cid:
            return c
    return None


def _clean_user_fields(src):
    """按白名单 + 长度上限收字段；返回的 dict 只含玩家能写的那些。"""
    src = src if isinstance(src, dict) else {}
    out = {}
    for k, cap in USER_FIELD_MAX.items():
        text = " ".join(str(src.get(k) or "").split()) if k in (
            "name", "charName", "avatar") else str(src.get(k) or "").strip()
        out[k] = text[:cap]
    for k, (lo, hi) in _INT_FIELD_MAX.items():
        out[k] = _as_int(src.get(k), None, lo, hi)
    out["level_names"] = _level_names(src.get("level_names"))
    # 状态行为 / 怀孕开关是卡面表现，玩家可以自己定；其余见 USER_FIELD_MAX。
    out["stateBehavior"] = _norm_state_behavior(src.get("stateBehavior"))
    out["pregnancyEnabled"] = bool(src.get("pregnancyEnabled", True))
    # 是否在一起：v2 的 50/75 两档靠它解锁，是个布尔而不是数值。
    out["start_together"] = bool(src.get("start_together"))
    return out


def _name_taken_error(qq, name, char_name, skip_id=""):
    """重名就拒绝，不做优先级。

    「官方 > 站主 > 玩家」那套排序要改 `find_card` 的匹配顺序、还要让玩家猜
    为什么自己的卡搜不到；创建时就拒（错误文案直接说找谁）便宜得多。
    卡名和「她真正的名字」两个都算——它们都会进 `find_card`。
    """
    if not str(name or "").strip():
        return "卡名没填"
    want = {str(name).strip().lower(), str(char_name or "").strip().lower()}
    want.discard("")
    pool = ([(c, "官方角色") for c in load_cards()]
            + [(c, "你自己的角色") for c in load_user_cards(qq)])
    for c, where in pool:
        if c["id"] == skip_id:
            continue
        has = {str(c.get("name") or "").strip().lower(),
               str(c.get("charName") or "").strip().lower()}
        has.discard("")
        clash = sorted(want & has)
        if clash:
            return "名字「%s」和%s的卡撞了，换一个" % (clash[0], where)
    return ""


def make_user_card(qq, src):
    """新建一张自建卡 → `(卡, "")`；不合法 → `(None, 错误文案)`。

    id 由后端发（`u<qq>_<uuid8>`），前端传什么都不认——归属不能由客户端声明。
    """
    owner = _digits_only(qq)
    if not owner:
        return None, "没登录，建不了角色"
    card = _clean_user_fields(src)
    err = _name_taken_error(owner, card["name"], card["charName"])
    if err:
        return None, err
    mine = load_user_cards(owner)
    if len(mine) >= USER_CARD_MAX:
        return None, "你自己的角色已经有 %d 张了，先删一张再建" % USER_CARD_MAX
    import uuid
    card["id"] = "u%s_%s" % (owner, uuid.uuid4().hex[:8])
    card["owner"] = owner
    card["createdAt"] = int(time.time() * 1000)
    if not _write_user_cards(owner, mine + [card]):
        return None, "存不下来，稍后再试"
    log.info("自建卡新建：%s → %s（%s）", owner, card["name"], card["id"])
    return _norm_card(card), ""


def edit_user_card(qq, cid, patch):
    """改自己的一张自建卡（只覆盖 patch 里点名的字段）→ `(卡, "")`。"""
    owner = _digits_only(qq)
    cid = str(cid or "").strip()
    if not owner or user_card_owner(cid) != owner:
        return None, "只能改你自己的角色"
    mine = load_user_cards(owner)
    hit = None
    for c in mine:
        if c["id"] == cid:
            hit = c
            break
    if hit is None:
        return None, "没有这张卡：%s" % cid
    upd = _clean_user_fields({**hit, **(patch or {})})
    err = _name_taken_error(owner, upd["name"], upd["charName"], skip_id=cid)
    if err:
        return None, err
    keep = {"id": hit["id"], "owner": owner, "createdAt": hit["createdAt"]}
    hit.clear()
    hit.update(keep)
    hit.update(upd)
    if not _write_user_cards(owner, mine):
        return None, "存不下来，稍后再试"
    return _norm_card(hit), ""


def remove_user_card(qq, cid):
    """删自己的一张自建卡。**只删这一张**，不做整表覆盖以外的任何事。

    正在演这张卡的会话不受影响：线键/历史/小记都按 id 存在 `persona_sessions.json`
    和 `sessions/` 里，卡没了 `active_card()` 就返回 None（沿用官方卡删除的既有行为）。
    """
    owner = _digits_only(qq)
    cid = str(cid or "").strip()
    if not owner or user_card_owner(cid) != owner:
        return False, "只能删你自己的角色"
    mine = load_user_cards(owner)
    left = [c for c in mine if c["id"] != cid]
    if len(left) == len(mine):
        return False, "没有这张卡：%s" % cid
    if not _write_user_cards(owner, left):
        return False, "删失败，稍后再试"
    log.info("自建卡删除：%s → %s", owner, cid)
    return True, ""


# ─── 会话状态 ────────────────────────────────────────

_STATE_PATH = os.path.join(BASE_DIR, "state", "persona_sessions.json")

def _state_guard(fn):
    """兼容旧引用：把状态文件的读写串行化。"""
    return state_store.state_guard(fn)


def _read_state_raw():
    """兼容旧引用：直接读盘，不迁移、不落盘。"""
    return state_store.read_raw(_STATE_PATH)


def _load_state():
    """读整份状态；分线迁移有改动就顺手落盘。"""
    with state_store.STATE_LOCK:
        state = state_store.load(_STATE_PATH)
        if state and _migrate_private_lines(state):
            _save_state(state)
        return state


def _save_state(state, deleted=None):
    """整份落盘，但先合并盘上别的线；天数和总轮次只许前进。"""
    state_store.save(_STATE_PATH, state, deleted, read_raw_fn=_read_state_raw)


def _migrate_private_lines(state):
    """把分线之前的 QQ 私聊状态搬到新布局。

    旧布局：`private_<qq>` 上挂整份状态 + 一份**所有角色共用**的历史。
    新布局：`private_<qq>__<card>` 持有那条角色线的真实状态，`private_<qq>`
    只留副本（`resolve_line` 靠它找线）。历史/归档文件同步改名。

    幂等：线键已存在就只对齐副本；没有 card 的键（出过戏）不动。
    """
    changed = False
    for key in [k for k in state
                if k.startswith("private_") and "__" not in k]:
        cid = str((state[key] or {}).get("card") or "").strip()
        if not cid:
            continue
        line = card_key(key, cid)
        if line not in state:
            state[line] = dict(state[key])
            _migrate_persona_files(key, line)
            changed = True
        if state.get(key) != state.get(line):
            state[key] = dict(state[line])
            changed = True
    return changed


def _migrate_persona_files(old_key, new_key):
    """历史 + 归档文件搬到新线键名下。目标已存在时不覆盖（防御）。"""
    for maker in (_hist_path, _archive_path):
        try:
            src = maker(old_key)
            dst = maker(new_key)
        except Exception:                      # noqa: BLE001
            continue
        if (src and dst and os.path.isfile(src)
                and not os.path.exists(dst)):
            try:
                os.rename(src, dst)
                log.info("分线迁移：%s → %s",
                         os.path.basename(src), os.path.basename(dst))
            except OSError:
                log.exception("分线迁移失败 %s", src)


def _erase(session_key):
    """把这条线的存档整份抹掉，**保留角色卡本身**。

    抹四样：
      1. 历史文件 `sessions/persona_<key>.jsonl`（整份删掉）；
      2. 归档 `sessions/persona_archive_<key>.jsonl`（同删，否则网页还能翻出来）；
      3. 状态里的 `note` / `turns` / `mood` / `intensity` / `state`；
      4. 好感记录整份换新（`affection.wipe_numbers` → 回到**这张卡的起点**，
         成就、结局、连续天数、机会次数一起清）。

    不动 `card`：要删的是这段关系，不是这个人。想换角色发另一个 `#角色名`，
    想退出扮演发 `#出戏`。

    历史文件删不掉也照常往下走，只在日志里记一笔 —— 命令失败不该让用户卡在
    那儿干等。

    返回角色名；这条线没在演任何人时返回 None。
    """
    state = _load_state()
    ent = state.get(session_key)
    if not isinstance(ent, dict):
        return None
    card = get_card(str(ent.get("card") or ""))
    name = char_name(card, key=session_key)

    # 1) 历史：整份删。删失败就清空成空文件，至少别让下一轮读到旧内容。
    #    QQ 私聊分线（2026-11-03）：传 base 键时先换算成当前角色线——
    #    删的是**现在演的这个角色**的存档，不碰别的线。
    line = resolve_line(session_key)
    path = _hist_path(line)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            log.exception("清空历史失败 %s", path)
            try:
                from app.core.agents import _atomic_write_text
                _atomic_write_text(path, "")
            except Exception:
                log.exception("清空历史时连写空文件都失败 %s", path)

    # 2) 归档：**必须一起删**（2026-11-04）。网页回看历史改成读归档之后，
    #    只删窗口的话「清空存档」就变成假删——聊天记录还能整段翻出来。
    ap = _archive_path(line)
    if ap and os.path.exists(ap):
        try:
            os.remove(ap)
        except OSError:
            log.exception("清空归档失败 %s", ap)

    # 2.5) 长期记忆向量库（2026-11-05）：只删**这一条线**的 collection，
    #      别人的用户/角色不受影响。后台队列执行；这期间该线检索被 blocked，
    #      不会把刚清掉的旧回忆又翻出来。
    try:
        persona_rag.drop_line(line)
    except Exception:                                    # noqa: BLE001
        log.exception("清空回忆库失败 %s", line)

    # 3) 好感：交给 affection 整份换新，它自己读盘写盘。
    #    必须在下面读 `_entry` **之前**做 —— 反过来的话，这层拿的是旧记录，
    #    最后整份 `_save_state` 会把刚换新的好感又盖回旧值。
    affection.wipe_numbers(line)

    # 4) 扮演态：保留 card，其余归零
    ent = _entry(line)
    if not isinstance(ent, dict):
        return None
    ent.update({"note": "", "turns": 0, "mood": "", "intensity": 0, "state": "",
                "intro": ""})     # 开场导读一起清：下一屏要按新起点重铺
    pregnancy.clear(ent)
    state = _load_state()
    state[line] = ent
    if line != session_key:
        # 输入是 base：base 上的副本也刷新，管理页/状态查询别显示旧小记
        state[session_key] = dict(ent)
    _save_state(state)
    log.info("存档已删除：%s（保留角色 %s）", session_key, ent.get("card"))
    return name


def clear(session_key):
    """把这条线的记忆清干净，**保留当前角色**（网页端「清空记忆」按钮走这里）。

    2026-11-05 起 QQ 侧**不再暴露成命令**（用户实测「清空」无确认直接删档，
    不可接受）。QQ 的命令入口统一走 `wipe_request` 的两步确认；这个函数
    只留给明确点了按钮的网页端。
    """
    name = _erase(session_key)
    if name is None:
        return "现在没有在演谁，没什么可清的。发「#角色名」挑一个吧。"
    return "记忆清空了，就当今天第一次见面。还是%s，想换人就发「#角色名」。\n" % name


def wipe(session_key):
    """「清空存档」执行体：真删。

    2026-11-04 用户拍板：老版本这里叫「重新开始」，但只清数值、聊天记忆留着，
    玩家删完发现她还记得上周聊的，觉得「没惩罚」。所以改成真删，并且
    改名「清空存档」+ 二次确认（见 `wipe_request`）。
    """
    name = _erase(session_key)
    if name is None:
        return None           # 没在演谁：当普通消息
    return ("🚨 存档已删除。\n"
            "你们的聊天记录、好感、关系进度、成就、连续天数全部清掉了，"
            "一切回到这张卡的最初。\n"
            "她还在这儿 —— 想继续就跟她说话，想换人发「#角色名」。")


# ── 「清空存档」的两步确认（2026-11-04，2026-11-05 加引用闸门）─────
# 网页那边是输入框 + 确认按钮；QQ 没有按钮，只能靠**引用那条警告再说一句话**：
#
#     玩家：清空存档（或单独发「清空」）
#     机器人：🚨 会删掉……真要删就**引用这条消息**，回一句「我同意清空存档」
#     玩家：［引用警告］我同意清空存档
#     机器人：🚨 存档已删除。
#
# 2026-11-05 用户实测：发「清空」没有确认就把记忆全删了；而且光回确认句
# 也拦不住手滑。所以现在确认**必须引用那条警告**——引用的 id 由 `qq_bot`
# 发完警告后回填（`note_wipe_prompt`），引用别的消息一律不认。
#
# pending 只存在内存里，进程重启就没了 —— 那是可接受的：玩家重发一次命令
# 而已，反正真删是不可逆的，宁可让他多确认一次。
WIPE_WORDS = ("清空存档", "删除存档", "重新开始", "清空")
WIPE_CONFIRM = "我同意清空存档"
_WIPE_TTL = 120.0
# session_key -> {"exp": 过期时间戳, "ids": [警告消息 id, ...]}
_WIPE_PENDING = {}


def wipe_request(session_key):
    """第一步：亮红灯 + 说清删什么，等玩家**引用这条**回确认句。"""
    # 传进来的可能是基础键（`private_123`），存档实际挂在分线键上；
    # 两种都查一下，免得「没在演」误判成普通消息。
    if not (_entry(session_key) or _entry(resolve_line(session_key))):
        return None           # 没在演谁：当普通消息，别拿命令去吓人
    card = active_card(session_key) or {}
    name = _card_label(card)
    _WIPE_PENDING[session_key] = {"exp": time.time() + _WIPE_TTL, "ids": []}
    return ("🚨 清空存档 —— 删掉就找不回来了。\n"
            "\n"
            "会删掉和「%s」的全部内容：\n"
            "· 聊天记录\n"
            "· 好感、关系进度、气氛\n"
            "· 成就、连续天数、剩下的机会次数\n"
            "\n"
            "不会删角色卡本身，她还在这儿，只是从头认识你。\n"
            "\n"
            "真要删，就**引用这条消息**，回一句：\n"
            "%s\n"
            "\n"
            "（两分钟内有效；回别的就当你改主意了）" % (name, WIPE_CONFIRM))


def note_wipe_prompt(session_key, ids):
    """把红灯消息的 id 记进 pending（`qq_bot` 发完就回填）。

    拿不到 id（协议端不回传）时 pending 里仍是空表，`wipe_confirm` 会退回
    「只认确认句」的老路径——宁可少一道闸门，也不能让用户完全删不掉。
    """
    p = _WIPE_PENDING.get(session_key)
    if not isinstance(p, dict):
        return
    p["ids"] = [str(i) for i in (ids or []) if i]


def wipe_confirm(session_key, quoted_ids=None):
    """第二步：验证**引用的就是那条警告** + 未过期，才真删。"""
    p = _WIPE_PENDING.get(session_key)
    if not isinstance(p, dict) or not p.get("exp"):
        return None           # 没有待确认：这句当普通聊天
    if p["exp"] < time.time():
        _WIPE_PENDING.pop(session_key, None)
        return None           # 过期了：也当普通聊天
    need = p.get("ids") or []
    got = [str(i) for i in (quoted_ids or []) if i]
    if need and not any(i in need for i in got):
        # 不 pop：留着让他在两分钟内引用对的消息重试
        return ("⚠️ 还没生效：要**引用我上面那条「清空存档」的警告**，"
                "再发「%s」。\n（两分钟内有效；回别的就当你改主意了）"
                % WIPE_CONFIRM)
    _WIPE_PENDING.pop(session_key, None)
    return wipe(session_key)


def is_wipe_request(text, addressed=False):
    """这条消息是不是删档第一步（`qq_bot` 发完要回填消息 id）。

    2026-11-05 起和其它命令一样要前缀；确认句不走这里（见 `wipe_confirm`）。
    """
    return _command_body(text, addressed) in WIPE_WORDS or \
        _command_body(text, addressed) in CLEAR_WORDS


def save_char_name(session_key):
    """这条**存档**自定义的女主名字；没设过返回空串（2026-11-09）。"""
    try:
        ent = _entry(resolve_line(session_key)) or {}
    except Exception:                                     # noqa: BLE001
        return ""
    return str(ent.get("char_name") or "").strip()


def set_char_name(session_key, name):
    """设置这条存档的女主名字；空串 = 恢复用卡名。返回最终生效的名字。"""
    key = resolve_line(session_key)
    state = _load_state()
    ent = state.get(key)
    if not isinstance(ent, dict):
        return ""
    nm = " ".join(str(name or "").split())[:20]
    if nm:
        ent["char_name"] = nm
    else:
        ent.pop("char_name", None)
    state[key] = ent
    _save_state(state)
    return nm


def char_name(card, default="她", key=None):
    """她**真正的名字**：存档自定义 > 卡上的 `charName` > 卡名。

    2026-11-09 群友反馈：卡名是「青梅竹马」这种关系称谓，AI 写出来像代号；
    想让正文用她真正的名字（苏暖）。两层可改：
      · 玩家在网页右栏「她的名字」改的是**这条存档**（state.char_name）；
      · 站主在角色管理里填的 charName 是这张卡的默认。
    卡名继续负责 `#命令` 匹配、列表和 LoRA，只有「叙述里怎么叫她」换名字。
    """
    if key:
        nm = save_char_name(key)
        if nm:
            return nm
    card = card if isinstance(card, dict) else {}
    return str(card.get("charName") or card.get("name") or default).strip()


def _card_label(card):
    """角色卡里能给人看的名字（有自定义名字就用自定义的）。"""
    return char_name(card)


def _entry(session_key):
    st = _load_state().get(session_key)
    return st if isinstance(st, dict) else None


def active_card(session_key):
    """这条会话线正在演谁（返回卡 dict）；没在演、或卡已经被删了 → None。

    传 base 键（QQ 的 `private_<qq>`）也行——内部先 `resolve_line` 换成
    当前线键再查。卡被删掉时自动当作没在演：不然会一直卡在一个取不到的
    卡 id 上，每轮都拼不出人设，还只能靠 `#出戏` 自救。
    """
    st = _entry(resolve_line(session_key))
    if not st:
        return None
    cid = str(st.get("card") or "").strip()
    if not cid:
        return None
    return get_card(cid)


def resolve_line(session_key):
    """base 键 → 此刻该用的**线键**（QQ 私聊分线，2026-11-03 方案 B）。

    网页端调用方（persona_api）传进来的已经是线键（带 `__`），原样返回；
    QQ 端传的是 base（`private_<qq>`），按 base 上记的 `card` 派生出
    `private_<qq>__<角色>`。这样：

      · 切角色 → 各聊各的线，历史/小记/好感互不污染（和网页一致）
      · 切回来 → 接着原来那条线聊
    群聊不分线（已停演），没在演时返回 base 本身。
    """
    key = str(session_key or "")
    if "__" in key or not key.startswith("private_"):
        return key
    ent = _entry(key) or {}
    cid = str(ent.get("card") or "").strip()
    if not cid:
        return key
    # 存档槽（2026-11-08）：base 上记着 slot 就派生出带后缀的线键。
    # 没有这个字段的老档按 slot 0 走，行为完全不变。
    try:
        slot = int(ent.get("slot") or 0)
    except (TypeError, ValueError):
        slot = 0
    return _card_key_slot(key, cid, slot)


def status_brief(session_key):
    """每轮跟在角色回复后面的「此刻」窄条（2026-11-05 用户要求）。

    和手动「状态」的分工：那边是**完整阶梯表**；这边只报箭头 ▶ 指着的那
    一格，免得每轮都把表格铺一遍。没在演（取不到线）返回 ""，调用方别拼。
    """
    try:
        return affection.panel_brief(resolve_line(session_key))
    except Exception:                            # noqa: BLE001
        log.warning("生成「此刻」简报失败 %s", session_key, exc_info=True)
        return ""


def switch_line(base, card, slot=0):
    """切角色 / 切存档（2026-11-08 加 slot）：**两条都要写**——

      · 线键 `base__<card_id>`：这条角色线的真实状态（历史/小记/好感都挂它）
      · base：记「此刻在演谁」的副本（`resolve_line` 靠它找线）

    切回一条演过的线时，`switch()` 会保留它的小记/轮次——真正「接着聊」。
    网页端 switch 端点与本函数是同一套语义（2026-11-03 起共用这里）。
    """
    ckey = _card_key_slot(base, card["id"], slot)
    switch(ckey, card)
    st = _load_state()
    st[base] = dict(st.get(ckey) or {})
    # base 上记「此刻是这张卡的第几个存档」——`_active_key` / `resolve_line`
    # 靠它派生出线键。slot 0 不写（老档没有这个字段，行为不变）。
    try:
        n = int(slot or 0)
    except (TypeError, ValueError):
        n = 0
    if n:
        st[base]["slot"] = n
    else:
        st[base].pop("slot", None)
    _save_state(st)
    return ckey


def switch(session_key, card):
    """进扮演态（`session_key` 应传**线键**，见 `switch_line`/`resolve_line`）。

    分线后的语义（2026-11-03 方案 B）：
      - **新线**（这个角色在这条会话没演过）→ 小记/轮次/心情全空白；
      - **切回旧线**（条目已存在且 card 相同）→ **原样保留**小记/轮次/心情
        ——「切回来接着原来那条线聊」，和网页的承诺一致。
        （旧版这里无条件清零，网页切回来小记也被洗掉，是同一处疏漏。）

    「上一个角色的记忆不该带进来」由**分线本身**保证：每条角色线有自己的
    小记和历史，换人 = 换线，天然互不污染。
    """
    state = _load_state()
    old = state.get(session_key)
    if isinstance(old, dict) and str(old.get("card") or "") == card["id"]:
        log.info("扮演态回到旧线：%s（保留小记 %d 字 / %s 轮）",
                 session_key, len(str(old.get("note") or "")),
                 old.get("turns"))
        return
    state[session_key] = {"card": card["id"], "note": "", "turns": 0,
                          "mood": "", "intensity": 0, "state": ""}
    _save_state(state)
    log.info("扮演态开启：%s → %s（%s，新线）",
             session_key, card["name"], card["id"])


def leave(session_key):
    """退出扮演态。历史留在盘上不管——下次演同一个角色还能接着聊。"""
    state = _load_state()
    had = state.pop(session_key, None)
    if had is not None:
        _save_state(state, deleted={session_key})
        log.info("扮演态结束：%s（原 %s）", session_key, had.get("card"))


# ─── 命令 ────────────────────────────────────────────

def roster_text(site_url=""):
    """`#角色扮演` / `#人设列表` 的回话：完整规则 + 角色名单。"""
    return roster.text(load_cards(), site_url)


def roster_nodes(limit_per_node=400, site_url=""):
    """把规则拆成「聊天记录卡片」的节点（2026-11-03）。

    为什么要拆：完整规则一千多字，直接发会刷屏、还会被 QQ 折成一坨。
    合并成卡片，点开才展开，和角色卡同一套做法。

    按 `【小节】` 切，每段不超过 `limit_per_node` 字——太长单张卡片里也难读。
    """
    text = roster_text(site_url=site_url)
    if not text:
        return []
    # 按小节标题切段，每段独立成一个节点
    parts, buf = [], []
    for line in text.splitlines():
        if line.startswith("【") and buf:
            parts.append("\n".join(buf).strip())
            buf = [line]
        else:
            buf.append(line)
    if buf:
        parts.append("\n".join(buf).strip())

    # 合并过短的段，避免卡片里出现只有标题的节点。
    #
    # 2026-11-09：第一段带网页入口时**不让后面的小节并进来**——它就单独做
    # 卡片的第一个节点，点开第一屏就能看到链接（用户要求「放最上面」）。
    # 群里没有链接，第一段还是标题行，照旧往下合并。
    merged = []
    for p in parts:
        head_is_entry = len(merged) == 1 and "http" in merged[0]
        if merged and not head_is_entry and len(merged[-1]) + len(p) < limit_per_node:
            merged[-1] = merged[-1] + "\n\n" + p
        else:
            merged.append(p)
    return [m for m in merged if m]


# 命令前缀：群里手打「@大大怪 #角色名」时，`#` 前面还有东西。
#
# 只在 `addressed` 为真时才剥（真 @，或者句子里喊了名字），所以私聊里
# 「我#tag 了一下」这种不会被误判成命令。抄的是 `galgame._PREFIX_RE`——
# 那边早就踩过同一个坑（2026-11-02 群友反馈 `@大大怪 gal 开局` 没反应）。
_PREFIX_RE = re.compile(
    r"^\s*(?:@[^\s，,、:：]+|" + re.escape(trigger.NAME) + r")\s*[，,、:：]?\s*")


def _match_card_in_text(text):
    """在一段描述里找卡名，返回命中的第一张卡（找不到返回 None）。

    场景：「角色扮演，你是我的白色小兔妹妹，我们相依为命……」——用户没写
    精确卡名，而是用一段话描述他要谁。这里按**卡名出现位置**找，
    **长得越长的名字优先**（「妹妹闺蜜」要压过「妹妹」）。
    """
    t = str(text or "")
    if not t:
        return None
    hits = []
    for c in load_cards():
        name = str(c.get("name") or "").strip()
        if not name:
            continue
        pos = t.find(name)
        if pos >= 0:
            hits.append((pos, -len(name), c))
    if not hits:
        return None
    hits.sort(key=lambda x: (x[0], x[1]))
    return hits[0][2]


def _group_redirect(card):
    """群里不让开演，回一段说明 + 去处。

    2026-11-03 用户反馈「群里其他人能看到别人的聊天内容」——状态隔离没坏，
    坏的是**群聊内容本来就公开**。所以群里不再实际扮演。
    """
    lines = [
        "这里人多，**不方便演**——我说的话一屋子人都看得到。",
        "",
        "想跟她单独聊，两个地方：",
        "",
        "1. **私聊我**，发「%s」就能开始" % card["name"],
    ]
    try:
        from app.features.qq import friend_site
        site = friend_site.url()
    except Exception:                              # noqa: BLE001
        site = ""
    if site:
        lines.append("2. **网页上玩**（能点按钮出图、看她的状态）：")
        lines.append("   %s/persona" % site.rstrip("/"))
    else:
        lines.append("2. **网页上玩**：找我主人要个地址")
    lines.append("")
    lines.append("（群里发「#角色扮演」还是能看到完整规则的。）")
    return "\n".join(lines)


def _command_body(text, addressed=False):
    """剥掉 @/名字前缀后取命令正文。**必须带 `#` 或 `/` 前缀**。

    2026-11-05 用户改口：之前为了「大家不爱打井号」把前缀做成了可选，
    结果群里聊到「妹妹」「清空」这种日常词也可能被当成命令。现在统一
    要求前缀，`#` 和 `/` 都认；不带前缀一律当普通聊天。
    """
    t = _strip_prefix(text, addressed)
    if not t or t[0] not in "#/":
        return None
    body = t[1:].strip()
    return body or None


def is_roster_request(text, addressed=False):
    """这条消息是不是在要「规则 / 名单」（`#角色扮演` / `/角色扮演`）。

    单独一个函数是因为 `handle_command` 只能返回一段文字，而调用方需要知道
    「这段文字要不要用聊天记录卡片发」（规则一千多字，直接发会刷屏）。
    前缀判据和 `handle_command` 共用 `_command_body`，别再各写一套。
    """
    return _command_body(text, addressed) in LIST_WORDS


def is_affection_query(text, addressed=False):
    """这条消息是不是在查「好感」档案（要用聊天记录卡片发）。"""
    return _command_body(text, addressed) in AFF_WORDS


def is_achievement_query(text, addressed=False):
    """这条消息是不是在查「成就」。同 `is_affection_query`，也要走卡片。"""
    return _command_body(text, addressed) in ACH_WORDS


def is_status_query(text, addressed=False):
    """这条消息是不是在查「状态面板」（2026-11-04 深夜）。

    同 `is_affection_query` 的道理：要把整张表画成聊天记录卡片，
    直接发一条长消息会把聊天刷没。
    """
    return _command_body(text, addressed) in STATUS_WORDS


def _strip_prefix(text, addressed=False):
    """把开头的 `@某人` / 「大大怪」剥掉（最多三次，叠着喊的也认）。

    只在 `addressed` 为真时剥——私聊里「我@某人 #tag」不该被当成命令。
    """
    t = str(text or "").strip()
    if not addressed:
        return t
    for _ in range(3):
        stripped = _PREFIX_RE.sub("", t, count=1).strip()
        if stripped == t:
            break
        t = stripped
    return t


def is_rollback_text(text, addressed=False):
    """正文是不是「回退」命令（2026-11-04）。

    只判正文整词；引用匹不匹配机器人上一条回复由 `qq_bot` 侧把关——
    单发「回退」两个字永远不是命令，落回普通聊天。2026-11-05 起也要带
    `#` / `/` 前缀（`#回退`、`/撤回`），和其它命令一致。
    """
    return _command_body(text, addressed) in ROLLBACK_WORDS


def handle_command(session_key, text, addressed=False, quoted_ids=None):
    """认扮演命令。返回要回的话；**不是命令返回 None**（照常往下走）。

    2026-11-05 用户改口：所有命令**必须带 `#` 或 `/` 前缀**，避免日常聊天
    里的「妹妹」「清空」「好感」被误触。认这几种形态：

      - `#和泉纱雾` / `/和泉纱雾`           → 进角色
      - `#角色扮演` / `/角色扮演`           → 列清单
      - `#出戏` / `/出戏`                   → 退出
      - `#角色扮演，你是我的白兔妹妹…`      → 进角色（命令词后面带描述也认）

    ⚠️ 例外只有一个：删档第二句的确认句（「我同意清空存档」）本身不是
    打命令，而是**引用警告后回的一句话**，不要求前缀——它已经有引用闸门。

    `addressed`（2026-11-03）：群里这条消息**确实在叫机器人**（真 @，或者
    句子里喊了「大大怪」）。为真时先把开头的 `@某人` / 名字剥掉——
    不剥的话「@大大怪 #青梅竹马」因为不以 `#` 开头，整条命令被当成普通
    聊天，落到 direct_gen 被当成画面需求。

    `quoted_ids`（2026-11-05）：这条消息引用到的消息 id 列表。只有「清空存档」
    的第二步用得上——确认句必须**引用它那条警告**才生效。
    """
    t = _strip_prefix(text, addressed)
    if not t:
        return None

    # 删档确认句：不要求前缀（见 docstring）。引用闸门在 `wipe_confirm` 里。
    if t == WIPE_CONFIRM:
        if not str(session_key or "").startswith("private"):
            return None
        return wipe_confirm(session_key, quoted_ids)

    body = _command_body(text, addressed)
    if body is None:
        # 没带前缀的普通聊天：上一步的删档确认作废。不这样的话，玩家
        # 12:00 发了「#清空存档」又反悔聊了两句，12:01 引用警告回一句
        # 「我同意清空存档」就真删了——两分钟的窗口不该这么宽。
        _WIPE_PENDING.pop(session_key, None)
        return None

    if body in LIST_WORDS:
        # 私聊才带网页入口（2026-11-03 用户拍板）。群里发外链容易被风控、
        # 而且一次就刷屏，所以群里只回规则本身。
        url = ""
        if str(session_key or "").startswith("private"):
            try:
                from app.features.qq import friend_site
                url = friend_site.url()
            except Exception:                      # noqa: BLE001
                url = ""
        return roster_text(site_url=url)
    if body in EXIT_WORDS:
        if not _entry(session_key):
            return None                     # 本来就没在演，当普通消息
        leave(session_key)
        return "出戏了，我又变回%s了。" % QQ_BOT_NAME

    if body in CLEAR_WORDS:
        if not (_entry(session_key) or _entry(resolve_line(session_key))):
            return None                     # 没在演时不算命令，免得误触
        # 2026-11-05：不再直接删——和「清空存档」一样先亮红灯、要引用确认。
        return wipe_request(session_key)

    # ── 攻略模式命令（2026-11-03）────────────────────────
    # 只在私聊认：群聊不扮演（上面群聊分支会拦），而且「好感」这种词在
    # 群聊日常里太常见，整条消息才认已经够了，私聊再放行。
    # 词表见文件头 RAID_WORDS 一带；细节在 app/affection.py。
    if str(session_key or "").startswith("private"):
        line = resolve_line(session_key)
        if body in RAID_WORDS:
            return affection.set_mode(line, True)
        if body in FREE_WORDS:
            return affection.set_mode(line, False)
        if body in SKIP_WORDS:
            turn = skip_day(session_key)
            if turn is None:
                return None             # 没在演：当普通消息，不误触
            return turn.reply
        if body in WIPE_WORDS:
            return wipe_request(session_key)
        # 说了别的话：上一步的确认作废。不这样的话，玩家 12:00 发了
        # 「#清空存档」又反悔聊了两句，12:01 引用警告回一句
        # 「我同意清空存档」就真删了 —— 两分钟的窗口不该这么宽。
        _WIPE_PENDING.pop(session_key, None)
        if body in MERCY_WORDS:
            return affection.mercy(line)
        if body in AFF_WORDS:
            return affection.detail_text(line)
        if body in ACH_WORDS:
            return affection.achievement_text(line)
        if body in STATUS_WORDS:
            return affection.panel_text(line)

    # `#角色扮演 青梅竹马` / `#角色扮演，你是我的白色小兔妹妹`：
    # 剥掉命令词，剩下那段先当卡名找；找不到再看整条是不是卡名。
    target = body
    for w in LIST_WORDS:
        if body.startswith(w):
            rest = body[len(w):].strip(" ，,、:：")
            if rest:
                target = rest
            break

    card = find_card(target)
    if card is None and target != body:
        # 「角色扮演，你是我的白色小兔妹妹…」这种没写准卡名的，从描述里找。
        card = _match_card_in_text(target)
    if card is None:
        return None

    # ── 群聊不实际扮演，只做引导（2026-11-03 用户反馈）────────────
    #
    # 起因：群友在群里跟机器人聊，**其他群成员能直接看到全部内容**。
    # 状态隔离（`_persona_key`）一直是好的，隔离的是记忆和好感；但机器人
    # 发到群里的消息本来就是全群可见，这是 QQ 群聊的固有属性，压不住。
    #
    # 用户玩的又是亲密内容，公开出去不合适。所以群里不再开演，改成把话
    # 说清楚 + 给私聊/网页的去处。
    #
    # 群里发「角色扮演」看规则仍然允许（规则本身不敏感），只有真正「切进
    # 某个角色」这一步被拦下来。
    if str(session_key or "").startswith("group_"):
        return _group_redirect(card)

    was = active_card(session_key)
    # 分线切换（2026-11-03 方案 B）：写线键 + base 指针。切回演过的角色时
    # 那条线的小记/历史原样保留——「接着上次聊」。
    switch_line(session_key, card)
    if was and was["id"] == card["id"]:
        return "我现在就是%s呀。" % card["name"]
    # 默认攻略模式（2026-11-04 用户要求）：进来就先有一份好感记录，
    # 网页右栏 / QQ 尾行立刻有东西可看；顺手把玩法讲明白，别让玩家自己猜。
    try:
        affection.status(resolve_line(session_key))
    except Exception:                                  # noqa: BLE001
        log.warning("进入扮演时建好感记录失败 %s", session_key)
    return ("好，从现在起我是%s（攻略模式）。每轮回话末尾会带一行你俩现在的关系，"
            "想关掉就发「#自由模式」。想让我变回来就发「#出戏」。" % card["name"])


# ─── 一轮 ────────────────────────────────────────────

def _archive_path(session_key):
    """归档文件：完整历史，不受 HISTORY_LIMIT 限制。"""
    return history_store.archive_path(_hist_path(session_key), session_key)

def _append_archive(session_key, msgs, seed=None):
    """往归档里追几条；首次补写用当前窗口铺底。"""
    history_store.append(_archive_path(session_key), msgs, seed)

def _read_archive_file(session_key):
    """只读归档文件本身；缺失 / 读不了返回 []，不回退窗口。"""
    return history_store.read_rows(_archive_path(session_key))

def load_archive(session_key):
    """读**完整**归档（`persona_archive_*.jsonl`）。网页回看历史读的是这一份。

    2026-11-04：以前网页读的是 `_load_hist` 的 12 条滑动窗口，所以「超过 6 轮
    就翻不到更早的」——数据一直在归档里，只是接口没给。这里不加新存储，只把
    已有的那份读出来。

    没有归档（功能上线前的老线，或归档写失败）就退回窗口原文——那时能看到的
    只有窗口内的这些，救不回来。**不调用 `trim()`**：回看要全文，截到 900 字
    加个省略号就不是「回顾」了。
    """
    rows = _read_archive_file(session_key)
    if not rows:
        rows = [m for m in _load_hist(session_key)
                if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    # 2026-11-06：老归档里的 `【心声】` 标签只在这份**返回副本**上剥掉，
    # 网页回看和前情提取拿到的都是干净文字；磁盘上的归档原样保留。
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("role") == "assistant" and r.get("content"):
            r = dict(r)
            r["content"] = _strip_monologue_tag(r["content"])
        out.append(r)
    return out


def archive_len(session_key):
    """归档一共多少条。给 `chat` 回包算「这一轮是第几条」用。"""
    return len(load_archive(session_key))


def _archive_assistant_count(session_key):
    """归档里她说过多少条——老线补算「累计轮次」用。"""
    try:
        return sum(1 for m in load_archive(session_key)
                   if isinstance(m, dict) and m.get("role") == "assistant")
    except Exception:                                    # noqa: BLE001
        return 0


def turn_total(session_key):
    """这条线累计交流了多少轮（网页「轮次」显示用，2026-11-06 用户要求）。

    和小记压缩的 `turns` 分开：那个每 `NOTE_EVERY_TURNS` 轮被 `compress_note`
    清零，玩家真正想看的是总量。新线由 `_bump_turn` 维护 `turn_total`；
    老线没有这个字段时按归档里她的回复条数补算（只读不写盘）。
    """
    key = resolve_line(session_key)
    ent = _entry(key)
    if not isinstance(ent, dict):
        return 0
    if ent.get("turn_total") is not None:
        try:
            return max(0, int(ent.get("turn_total") or 0))
        except (TypeError, ValueError):
            return 0
    return _archive_assistant_count(key)


def history_page(session_key, before=None, limit=None):
    """归档里 `[before-limit, before)` 这一段，返回 (start, chunk, total)。

    `before=None` → 最后 limit 条（首屏）；否则往前翻一页。
    下标就是归档**行号**，也是前端气泡的 id —— 归档只增不删，所以它稳定，
    不会像「窗口内第几条」那样聊一轮整体前移。
    """
    rows = load_archive(session_key)
    total = len(rows)
    try:
        end = total if before is None else int(before)
    except (TypeError, ValueError):
        end = total
    end = max(0, min(total, end))
    try:
        lim = int(limit or ARCHIVE_PAGE)
    except (TypeError, ValueError):
        lim = ARCHIVE_PAGE
    lim = max(1, min(200, lim))
    start = max(0, end - lim)
    return start, rows[start:end], total


def _hist_path(session_key):
    """扮演态的历史单独一份，**不跟生图机器人共用**。

    共用的话，切进角色时会先看到一串「银发少女 1024×1536 anima多图」，
    角色开口就串味；切出去以后生图机器人又得读一遍角色说过的话。
    文件名带 `persona_` 前缀，管理页里能一眼分出来。
    """
    from app.core import agents
    from app.core import agents as agent_store
    if not _SAFE_KEY.match(session_key or ""):
        return None
    d = agents.agent_dir(QQ_AGENT_ID)
    if not d:
        return None
    return os.path.join(d, "sessions", "persona_" + session_key + ".jsonl")


def _load_hist(session_key):
    """读滑动窗口原文（给模型看的那 12 条）。"""
    return history_store.read_window(_hist_path(session_key))

def _save_hist(session_key, msgs):
    """整份重写滑动窗口（原子写）。"""
    history_store.write_window(_hist_path(session_key), msgs)

def trim(hist, limit=None):
    """取最近 N 条（默认 HISTORY_LIMIT），单条超 MSG_MAX_CHARS 截断。

    `limit`（2026-11-10）：给前缀缓存的滞回窗口用。传窗口的实际长度时，
    **只做单条截断、不砍条数**，历史前缀才能逐轮只追加。不传时行为与
    以前完全一致（编剧 / 打分器 / 回看等内部调用仍只看最近 12 条）。

    裁的是**送进模型的副本**；落盘保留原文（2026-11-05 起，`run_turn` 直接存
    `_load_hist` 的原始窗口，不再把截断版写回去）——网页回看和归档要的是全貌。

    2026-11-02：assistant 那几条**保留结构化尾巴**。截断只砍正文，最后三行
    （画面 / 心情 / 状态）原样留着——它们是 few-shot 示范，历史里全是纯对话
    的话，模型第 2 轮起就会跟着历史把格式丢掉（实测第 1 轮完美、第 2 轮全空）。
    """
    msgs = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    n = HISTORY_LIMIT if not limit else max(1, int(limit))
    msgs = msgs[-n:]
    out = []
    for m in msgs:
        c = str(m.get("content") or "").strip()
        if len(c) > MSG_MAX_CHARS:
            if m["role"] == "assistant":
                body, tail = _split_meta(c)
                room = max(20, MSG_MAX_CHARS - len(tail) - 1)
                c = body[:room].rstrip() + "…" + tail
            else:
                c = c[:MSG_MAX_CHARS] + "…"
        out.append({"role": m["role"], "content": c})
        # 分段（2026-11-05）：送到模型前还要再渲染一次，所以 `trim` 不能把
        # `seg` 丢掉。展示/归档读的是 `content`，不受影响。
        if m.get("role") == "user" and m.get("seg"):
            out[-1]["seg"] = m["seg"]
            out[-1]["addressed"] = m.get("addressed", True)
    return out


def cache_rows(hist):
    """前缀缓存窗口：逐轮只追加，攒过 HISTORY_CACHE_HIGH 才一次性砍回低水位。

    返回的是**原始行**（带着 `seg` 等字段），既能送模型（外面再 `trim(...
    , limit=len(win))` 做单条截断），也能原样落盘。两条路径必须走同一个函数：
    落盘的内容要和上一轮发给模型的历史序列逐字节对得上，下一轮才接得上前缀。

    砍回那一次会断一次缓存，这是滞回的代价；换来的是其余 9 轮都能整段命中，
    比每轮滑动（每轮都断）划算得多。
    """
    rows = [m for m in (hist or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    if len(rows) > HISTORY_CACHE_HIGH:
        rows = rows[-HISTORY_LIMIT:]
    return rows


def _split_meta(text):
    """把一条 assistant 回复切成「正文」和「结构化的三行」。

    返回 `(正文, "\\n画面：…\\n心情：…\\n状态：…")`；没有结构化行时尾巴为空串。
    用行首匹配而不是别处出现的词，避免正文里提到「画面」被误切。
    """
    lines = str(text or "").split("\n")
    keep, idx = [], len(lines)
    # 从尾巴往回找连续的三类元数据行（允许中间夹空行）
    metas, i = [], len(lines) - 1
    while i >= 0:
        ln = lines[i].strip()
        if not ln or any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
            if ln and any(rx.match(ln) for rx in (_SCENE_RE, _MOOD_RE, _STATE_RE)):
                metas.insert(0, lines[i])
                idx = i
            i -= 1
            continue
        break
    if not metas:
        return str(text or ""), ""
    keep = lines[:idx]
    return "\n".join(keep).rstrip() + "\n", "\n" + "\n".join(metas)


# ── 框定（2026-11-06 用户要求「把破甲那套用到角色扮演」）──────────
#
# 从 dsh-purge 的注入文件里搬的是**思路**而不是原文：那套是给编码 agent 用的
# （工具、沙箱、审批），和角色扮演无关。真正可迁移的只有三点——
#   1. 先定框架（这是私下的虚构创作），模型才不会把自己当审核员；
#   2. 明确「输出里不许有元话语」——道歉、声明身份、建议换个话题都属于
#      跳出角色，比内容被拦更破坏体验；
#   3. 给一条**在角色内**的退路（沉默、转身、岔开），拒绝就不会变成
#      一句英文样板话砸在正文后面。
#
# 开关在 `config/persona.json` 的 `guard.enabled`，关掉就整段不拼。
# 2026-11-06 按「白象效应」改成正向：原来四条里三条是「不…」，等于把
# 「道歉 / 声明身份 / 跳出角色」这些念头一个个点亮一遍。现在只说要做什么。


def build_head(card, session_key, sender, note="", state="", intimate=True,
               mode="normal", dynamic=True):
    """扮演态的系统头；`dynamic=False` 把小记和状态留给末尾玩家消息。"""
    return head_builder.build(
        card, session_key, sender, char_name(card, key=session_key),
        note=note, state=state, intimate=intimate, mode=mode, dynamic=dynamic)


# ── 精确输入（2026-11-06 用户要求）──────────────────────────
# 主输入框仍然是兜底；玩家也可以明确分栏写「心理活动 / 动作 / 台词 / 剧情」。
# 分栏内容以玩家标注为准，编剧 AI 不再重拆。心理活动只进编剧层，绝不进演员层。
# 编剧的交付标准（2026-11-08 用户拍板）。
#
# 玩家写的是**指令**，不是剧本原文。他说「我把她拉起来」，意思是这一拍要
# 发生「他把人拉起来」这件事——不是「她已经被拉起来了」。实测：演员拿到
# 前者会直接写「她感觉到…」，动作整个省掉，因为没有人要求它写。
#
# 所以这一段必须跟着用户输入一起给编剧，而且要钉死「动作要进正文」。

# 近因位再钉一次原话（2026-11-10）：末尾玩家消息里，原话排在最前，后面
# 跟着近千字的 facts / 关系读数 / 状态 / 尾注，整条请求约 2.9 万字符时
# 它只占 0.1%。实测回复字数稳定在 900~1100，问题不是写不长，是没落在
# 这一句上。这里在生成点前把原话再放一份，并把「动作先落成画面」那句
# 贴回来（Phase B 把整段场景义务搬进 system 头后，贴身说明书离原话太远）。
# 纯追加：已有块的顺序一点不动，tail 仍是最后一句；落盘历史不含这一段，
# 所以滞回窗口的跨轮前缀也不受影响。


def _stable_note_blocks():
    """逐轮恒定的写法说明块（场景义务 + 道具清单）。

    2026-11-10 缓存 Phase B：这些字每轮一字不变，但原先拼在**末尾玩家消息**
    里。前缀缓存只命中「请求开头 → 第一个变化点」，末尾消息永远整段按 miss
    付；重排它在 user_block 内部也没用——下一轮同一位置已经被新追加的历史
    顶掉了。只有并进 system 头（本来就是逐字节稳定的前缀）才能变成命中。
    末尾那份由 `_strip_stable_notes` 剔除，所以文字总量不变。
    """
    try:
        from app.features.persona import items as _items
        listing = _items.list_block()
    except Exception:                                        # noqa: BLE001
        listing = ""
    return [b.strip() for b in (_SCENE_DUTY, listing) if b and b.strip()]


def _stable_note_segments():
    """`_stable_note_blocks()` 按空行拆成的整段集合，判定只认整段字面相等。"""
    out = set()
    for block in _stable_note_blocks():
        for seg in block.split("\n\n"):
            if seg.strip():
                out.add(seg.strip())
    return out


def _strip_stable_notes(user_block):
    """把已并进 system 头的恒定段从末尾玩家消息里去掉。

    只按「整段字面相等」剔除，不猜内容；判错顶多少命中一段，不影响语义。
    两条拼法都要盖住：`run_turn` 用空行连，`render_user_block` 的分段路径
    用单换行连——后者靠整块 replace 才删得掉，否则会重复付一份 miss。
    """
    stable = _stable_note_segments()
    text = str(user_block or "")
    for block in _stable_note_blocks():
        if block and block in text:
            text = text.replace(block, "")
    return "\n\n".join(
        s.strip() for s in text.split("\n\n")
        if s.strip() and s.strip() not in stable)


def _rag_context(hist, rounds=2):
    """给 RAG 的「最近几轮上下文」（2026-11-06）。

    只取用户可见话 + 她的正文，压成短串。短句「你还记得昨天吗」单独去检索
    语义太弱；带上最近两轮，能把「昨天在哪、和谁、发生了什么」带出来。
    """
    rows = [m for m in (hist or []) if isinstance(m, dict)]
    parts = []
    for m in rows[-(max(1, int(rounds or 1)) * 2):]:
        if m.get("role") == "user":
            # 用户消息优先用分段里的「说/做」；心里话和剧情方向不拿去检索。
            text = _visible_user_text(m.get("seg"), m.get("content"))
            who = "用户"
        else:
            text, _tail = _split_meta(str(m.get("content") or ""))
            who = "她"
        text = re.sub(r"\s{2,}", " ", text.replace("\r", "")).strip()
        if text:
            parts.append("%s：%s" % (who, text[:80]))
    return " / ".join(parts)


# ─── 意图判断（两段式第一段，2026-11-02）────────────────────
#
# 为什么拆成两段：原来一次调用里塞了人设、契约、亲密指导、格式要求，模型只能
# 挑最要紧的执行。实测同一句「做红烧兔头」，四个角色把它当成真的要做菜
# （学姐在教焯水、妹妹已经把兔头下锅了），而白色小兔因为设定里有兔子的身份，
# 反而听出了双关。缺的不是更长的人设，是「这是在开玩笑」这一层判断。
#
# 拆法：先用一段**短提示词、不带人设**的调用判断意图，把结论以一行注入到
# 主提示词里。判断段只有判断，表演段只有表演，互不抢注意力。
#
# 失败一律退化成 `None`：调用方不注入那一行，走原来的单次流程。分类挂了
# 不该让整轮回不了话。

# 意图类别。前三类是私聊的主要场景，joke 是这次要治的，insult 兜底。
INTENTS = {
    "intimate": "亲密/求欢：想亲近、调情、要求身体接触或性",
    "joke": "玩笑/逗你：拿角色开玩笑、威胁着玩、故意说离谱的话",
    "literal": "字面请求：真的要求做某件事、问某个问题",
    "insult": "冒犯：贬低、辱骂、羞辱角色本身",
    "neutral": "普通对话：闲聊、叙述、没有明确指向",
}

# ── 本轮事实点（2026-11-08）──────────────────────────────
# 玩家一段话里并列的道具、动作，最容易在第一段被「做→一句话概括」压成
# 「他拿出食材」，演员再写就只剩最后一句台词（拿出了鸡蛋，却被反问
# 「要不要加个鸡蛋」）。读原文的是编剧，所以让它顺手列一张清单；代码只
# 裁剪和透传，落地交给演员——和 segments / items 同一个分工。
#
# 清单还兼一个作用：契约里有「你话里交代的前因，她没跟着去过」，场景义务
# 又要求「他做的动作要演出来」。玩家写「我把鸡蛋拿出来」时模型会摇摆，
# 于是把摆好的东西当没发生。这里正面说清「就在这一幕眼前」，不动契约正文。


def _director_fields(d):
    """从第一段的 JSON 里取**事实**字段（2026-11-09 收窄）。

    这里只剩事实：这一轮已经摆出来的东西（facts）、他这句是不是对着她说
    （addressed）、他有没有藏着话（secret）、有没有内射（finish）、道具进出
    （items）。

    分段（说·做·想）、潜台词、她察觉到的、这一拍的态度、主动的勾子全部
    不再由编剧产出——那五样都是「这一拍该怎么写」的判断，编剧只该记数值
    和已经发生的事，写法交给写作层。
    """
    if not isinstance(d, dict):
        return {"facts": [], "addressed": True, "secret": False,
                "finish": "unknown", "items": None}
    addressed = d.get("addressed", True)
    if isinstance(addressed, str):
        addressed = addressed.strip().lower() not in ("false", "0", "no", "否", "不")
    else:
        addressed = bool(addressed)
    secret = d.get("secret")
    if isinstance(secret, str):
        secret = secret.strip().lower() in ("true", "1", "yes", "是")
    return {
        "facts": _parse_facts(d.get("facts")),
        "addressed": addressed,
        # 他说没说出口，是事实；内容不抄给演员（他看得见的是行为，不是心声）。
        "secret": bool(secret),
        # 怀孕（2026-11-05）：编剧判这一轮有没有内射。代码只在跨天时用。
        "finish": _norm_finish(d.get("finish")),
        # 道具（2026-11-08）：v1 / 自由模式也走这条路，透传出去。
        "items": d.get("items") if isinstance(d.get("items"), dict) else None,
    }


def _norm_finish(value):
    v = str(value or "").strip().lower()
    return v if v in ("inside", "outside", "none", "unknown") else "unknown"


# ── 射精选择（2026-11-05，用户拍板）────────────────────────
#
# 用户原话：「玩家他不需要自己去写，他只需要负责推动剧情就行了。女主来到
# 高潮之后，就给选项让玩家自己选，这样就可以避免玩家不知道什么时候去写
# 这个摄入会比较好——相当于系统帮玩家去做这个决定。」
#
# 所以流程是：
#   1. 高潮值满 → 前端弹窗问「射在哪里」；
#   2. 选择存进会话（`finish_pick`），**不由玩家写在正文里**；
#   3. 下一轮 `run_turn` 取走，拼成导演条交给编剧，由它写出来。
#
# 位置只有五种。`none` 是「不射」——那也是合法选择，不是没选。
FINISH_SPOTS = {
    "mouth": "嘴里",
    "body": "身上",
    "pussy": "小穴里",
    "anus": "肛门里",
    "none": "不射",
}


def set_finish(session_key, spot):
    """记下玩家选的射精位置。下一轮 `run_turn` 取走并交给编剧。"""
    key = resolve_line(session_key)
    spot = str(spot or "").strip().lower()
    if spot not in FINISH_SPOTS:
        return False
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return False
    ent["finish_pick"] = spot
    _save_state(st)
    return True


def take_finish(session_key):
    """取走并清空。没有就返回空串。"""
    key = resolve_line(session_key)
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return ""
    spot = str(ent.pop("finish_pick", "") or "").strip().lower()
    if not spot:
        return ""
    _save_state(st)
    return spot


# 「身上」最容易穿帮：上一轮是后入，就不能写成射在小腹正面。所以每个位置
# 都带一句「按你们现在的姿势找地方」，而不是给一个固定落点。
_FINISH_HINT = {
    "mouth": ("他射在她嘴里。写清她的反应——咽、吐、还是含着不动；"
              "嘴里含着东西时她说不出完整的句子，这一点要写出来。"),
    "body": ("他射在她身上。**落点要按你们这一轮的姿势定**："
             "背后进入/后入就落在后腰、臀、脊背；面对面才可能落在小腹、胸口；"
             "她跪着就落在脸、颈、胸口。别把落点和姿势写矛盾了。"
             "写她皮肤上的痕迹，以及她低头看见时的表情。"),
    "pussy": ("他射在里面。写到她感觉到的那一下——里面一热、腿根收紧、"
              "她下意识想夹住。之后不要把这件事略过去，"
              "她会知道那是什么，也会知道这意味着什么。"),
    "anus": ("他射在肛门里。写她后穴的反应——被灌进来时的那种撑和热，"
             "以及她整个人绷住的那一拍。这个位置对她更过界，"
             "所以她的反应要跟着更重，别写成和前面一样。"),
    "none": ("这一拍他没有射。可以写他停在里面、或者是退出来、或者只是抱着她缓；"
             "她已经到过了，身体还在余韵里。**不要把「没射」写得像错过了什么**——"
             "这只是一个选择。"),
}


def _set_pending_finish(key, info):
    """把「等着问射在哪里」这个待办记进存档（2026-11-06）。

    为什么需要它：高潮值是在**普通一轮**里结算的（`settle` 已经把它扣了），
    而「射在哪里」的弹窗是那一轮之后才弹的。如果玩家点确定之前页面刷新、
    断网、或者干脆关了标签，弹窗就没了——数值已经扣过，文本却没写。
    之前就是这么丢了那一段。

    所以把待办落盘：刷新后 `/api/persona/session` 会把它带回来，
    前端重新弹一次。玩家真写了（或者明确跳过）才清掉。
    """
    if not key:
        return
    try:
        ent = _entry(key) or {}
        if info:
            ent["pending_finish"] = dict(info)
        else:
            if "pending_finish" not in ent:
                return          # 本来就没有待办：不白写一次盘
            ent.pop("pending_finish", None)
        state = _load_state()
        state[key] = ent
        _save_state(state)
    except Exception:                                  # noqa: BLE001
        log.exception("待办射精落盘失败 %s", key)


def pending_finish(key):
    """这条线有没有一个「还没回答」的射精弹窗。"""
    try:
        ent = _entry(key) or {}
        v = ent.get("pending_finish")
    except Exception:                                  # noqa: BLE001
        return None
    if not isinstance(v, dict):
        return None
    try:
        n = int(v.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return None
    return {"n": n, "of": int(v.get("of") or 0), "last": bool(v.get("last"))}


def _climax_info(res):
    """这一轮是不是刚到高潮 / 是不是第 5 次。没到返回 None。

    前端拿到 `{n, of, last}` 就弹「射在哪里」；`last=True` 时答完再弹
    「结束 / 最后一次」。判定只用 `just_orgasm`——它由 `settle` 写，
    是「这一轮真的到了一次」的唯一事实来源。
    """
    try:
        n = int((res or {}).get("just_orgasm") or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return None
    try:
        omax = int((res or {}).get("orgasmMax") or 0)
    except (TypeError, ValueError):
        omax = 0
    return {"n": n, "of": omax, "last": bool(omax and n >= omax)}


def _clear_adult_night(key):
    """跨天把 v2 的高潮值和冷却清零（2026-11-10）。

    `affection.sleep()` 管的是时间跳跃 / 第二天早晨那两条显式跨天路径；
    编剧在聊天里判 `time=overnight` 走的是 `_apply_gameday`，以前**完全没
    收场**，第二天面板还挂着昨晚的高潮值。这里补上，和 sleep 同口径：
    只清 excite / recover，开发度那些「发生过的事实」不动。
    """
    try:
        st = _load_state()
        rec = st.get(key) or {}
        aff = rec.get("affection")
        adult = aff.get("adult") if isinstance(aff, dict) else None
        if not isinstance(adult, dict):
            return
        aff["adult"] = adult_state.night_reset(adult)
        _save_state(st)
    except Exception:                                    # noqa: BLE001
        log.exception("跨天清 v2 高潮值失败 %s", key)


def finish_block(spot):
    """把「射在哪里」拼成演员层的导演条。没有选择就返回空串。"""
    spot = str(spot or "").strip().lower()
    hint = _FINISH_HINT.get(spot)
    if not hint:
        return ""
    return ("## 这一拍：射精（系统已经替你决定了位置）\n\n"
            + hint
            + "\n\n（这是**已经发生的事实**，不要改成别的位置，"
              "也不要问她「要射哪里」——她已经在高潮的余韵里，"
              "这一拍是他自己的事。写她怎么承受、怎么反应。）")


def _judge_recent(hist):
    """给第一段看的「最近发生过什么」。只要正文，不要画面/心情/状态三行。"""
    rows = [m for m in (hist or []) if isinstance(m, dict)]
    if not rows:
        return "（还没有）"
    lines = []
    for m in rows[-10:]:
        role = "用户" if m.get("role") == "user" else "助手"
        body = str(m.get("content") or "").strip()
        if m.get("role") == "assistant":
            body, _tail = _split_meta(body)
        body = re.sub(r"\s{2,}", " ", body.replace("\r", "")).strip()
        if not body:
            continue
        lines.append("%s：%s" % (role, body[:300]))
    return "\n".join(lines) or "（还没有）"


def _judge_ledger(note):
    """给第一段看的长期账本。空账本就写「还没有」，别塞一个空标题。"""
    text = str(note or "").strip()
    return text[:NOTE_MAX_CHARS] if text else "（还没有）"



def _item_keys_text():
    """道具清单的 key 列表，给编剧挑 use 用。**只给 key，不给 prompt**。

    写在提示词里的是「有哪些可选」，具体怎么写由 `app/items.py` 在
    用到那一条时注入——这样十几样道具都进清单也只占一行。
    """
    try:
        from app.features.persona import items as _items
        rows = []
        for key, it in (_items.all_items() or {}).items():
            if isinstance(it, dict):
                rows.append("%s=%s" % (key, it.get("name") or key))
        return "、".join(rows) if rows else "（无）"
    except Exception:                                     # noqa: BLE001
        return "（无）"


def _judge_intent(card, said, state="", history=None, note=""):
    """判断这句话在干什么，并顺手记下这一轮的事实。返回 `(intent, note, extra)`。

    单独一次调用、不带人设——人设会影响判断（傲娇角色看什么都像调戏）。
    超时短、输出短，失败就退化成原流程，不阻塞这一轮。

    `extra` 是事实字段（facts / secret / addressed / items），不再含任何
    「这一拍怎么写」的判断。失败时是空 dict，调用方退回原文。
    """
    who = char_name(card, "对方")
    prompt = _INTENT_PROMPT % {
        "who": who + ("（" + state + "）" if state else ""),
        "said": str(said or "")[:2000],
        "recent": _judge_recent(history),
        "ledger": _judge_ledger(note),
        "facts_guide": _FACTS_GUIDE,
        "item_keys": _item_keys_text(),
    }
    try:
        # 2026-11-05：分段 + 导演条后输出变长，20s 偶发不够；放到 30s。
        # 判挂了只是退回原文，不会阻塞这一轮，但能判出来质量更好。
        raw = _llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("意图判断调用失败，走原流程")
        return None, "", {}
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        log.info("意图判断没交 JSON，走原流程：%r", str(raw or "")[:80])
        return None, "", {}
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        log.info("意图判断给了不认识的类别 %r，走原流程", intent)
        # 类别不认识不影响事实字段：只要它交了 facts / secret，照样能用。
        extra = _director_fields(d)
        if not (extra["facts"] or extra["secret"]):
            extra = {}
        return None, "", extra
    note = str(d.get("note") or "").strip()[:30]
    return intent, note, _director_fields(d)



# ── 攻略模式的第一段（2026-11-03）────────────────────
#
# 自由模式的第一段只判意图；攻略模式下同一次调用**顺带打分**：
# 意图（决定亲密指导拼不拼）+ 关系影响（delta / 剧情还是聊天 / 原因）。
# 一次调用干两件事，不多烧一轮。
#
# 评分标准（rubric）只是**指导不是硬规则**——斟酌权在模型。代码侧只做
# 夹单轮上限、降档滞回（见 `affection.settle`）。**按天的额度已于
# 2026-11-04 晚全部取消**：聊天和剧情同权，想调节奏改 ROUND_CAP，
# 不要往回合数上加时间闸。
#
# 「空口宣称的大事按聊天力度给分」这条是防刷分的核心：不然群友一句
# 「我把她救回家了」白嫖 +15。有没有铺垫和细节，模型读上下文能看出来。


def _score_profile(card, state="", key=""):
    """打分用的「她是谁」：名字 + 当前状态 + 性格 + 设定。

    2026-11-03：以前只传名字，等于所有卡共用一把尺子，谁都不难攻略。现在把
    性格和设定给打分模型，让它按**这个人的感受**判分值——黏人的角色靠近就
    加分，戒备的角色同样的靠近要扣分，难度自然长在卡上，而不是长在代码里。

    截断是为了控制这段的长度：personality 常见一两百字，description 可能
    几千字，全塞进去既慢又容易把「他说的」挤到后面被忽略。1600 字的卡
    前面这段就够模型认人了。
    """
    c = card if isinstance(card, dict) else {}
    name = char_name(c, "对方", key=key)
    lines = ["- 名字：" + name + (("（" + state + "）") if state else "")]
    p = str(c.get("personality") or "").strip()
    if p:
        lines.append("- 性格：" + p[:200])
    d = str(c.get("description") or "").strip()
    if d:
        lines.append("- 设定：" + d[:300])
    sb = _state_behavior_text(c)
    if sb:
        lines.append("- 状态下的她（不是阈值，是她高值时会怎么变）：\n" + sb)
    return "\n".join(lines)


# ── v2 正向关系线的第一段（2026-11-08）─────────────────────────
# 和上面那份 v1 打分提示词完全分开：v1 那套服务的是老档的距离层级 /
# 冲动 / 高潮 / 堕落 / 开发度，v2 一条都不碰。这里只判四条正向轴，
# 而且**每一轴都用中文把「这个数是什么意思」说清楚**——只扔英文字段名
# 给第二段演员，它根本不知道前面算的表白值代表什么，等于白算。


def _judge_v2(card, said, state="", line_key="", gameday=None,
              history=None, note=""):
    """v2 的第一段：意图 + 四条正向轴 + 分段/导演条。返回 `(intent, note, score)`。

    判断失败返回 `(None, "", None)`——按 0 分走，不让整轮回不了话。
    """
    gd = gameday if isinstance(gameday, dict) else {}
    plan = str(gd.get("plan") or "").strip()
    ctx = affection.scoring_ctx(line_key) or {}
    # 前缀缓存（2026-11-10）：模板里「稳定规则在前、这一轮事实在末尾」。
    # 早期版本把动态数值放在第 3 行，整段提示词每轮都变，hit 恒为 0；
    # 现在前半段逐字节不变，DeepSeek 能整段命中，只有末尾事实按 miss 付。
    # 调用仍是单条 user 消息，消息结构没动。
    prompt = _SCORE_PROMPT_V2 % {
        "relation_name": ctx.get("relation_name") or "正常关系",
        "relation_desc": ctx.get("relation_desc") or "",
        "together": ("你们已经在一起了。" if ctx.get("together")
                     else "还没在一起；没表白之前，关系档不会自己往前跳。"),
        "contact_name": ctx.get("contact_name") or "还没碰过",
        "confess": int(ctx.get("confess") or 0),
        "confess_max": int(ctx.get("confess_max") or 50),
        "confess_name": ctx.get("confess_name") or "还没想过",
        "confess_desc": ctx.get("confess_desc") or "",
        "yandere_name": ctx.get("yandere_name") or "没有",
        "dev": ctx.get("devText") or "（五处都还没被开发过）",
        "item_keys": _item_keys_text(),
        "recent": ctx.get("recent") or "（你们还没什么交集）",
        "gday": _day_of(gd),
        "gphase": str(gd.get("phase") or "还没定"),
        "gplan": _plan_fact(gd, plan),
        "profile": _score_profile(card, "", key=line_key),
        # 上一轮状态逐轮会变，从「她是谁」里拆出来留在事实段：
        # 卡面那段因此恒定，能整段吃前缀缓存；信息一点没少给。
        "state_line": ("她上一轮身上的状态：" + str(state) + "\n")
                      if str(state or "").strip() else "",
        "said": str(said or "")[:2000],
        "history": _judge_recent(history),
        "ledger": _judge_ledger(note),
        "facts_guide": _FACTS_GUIDE,
    }
    try:
        raw = _llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("v2 打分调用失败，按 0 分走")
        return None, "", None
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        log.warning("v2 没交 JSON，按 0 分走（共 %d 字）：头=%r 尾=%r",
                    len(str(raw or "")), str(raw or "")[:120],
                    str(raw or "")[-120:])
        return None, "", None
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = None
    note = str(d.get("note") or "").strip()[:30]
    # settle_v2 只认这个 dict，少带一项等于模型白判。
    score = {k: d.get(k) for k in (
        "delta", "turn_type", "reason", "contact", "confess", "finish",
        "yandere", "proposal", "accept", "refused_confession", "facts",
        "addressed", "secret", "time", "phase", "plan", "place")}
    # adult（2026-11-08）：身体这条线。**原样透传，不在这里净化**——
    # 结算器 affection_v2_adult.apply 负责 clamp/闸门/回落/去重，
    # 这里多改一处就多一个离线口径。
    if isinstance(d.get("adult"), dict):
        score["adult"] = d["adult"]
    # items（2026-11-08）：原样透传，净化在 app/items.py 里做。
    if isinstance(d.get("items"), dict):
        score["items"] = d["items"]
    return intent, note, score


def _judge_intent_scored(card, said, state="", line_key="", gameday=None,
                         history=None, note=""):
    """攻略模式的第一段：意图 + 关系打分 + 分段/导演条。返回 `(intent, note, score)`。

    判断失败时 `(None, "", None)`——退化成没判过（跟 `_judge_intent` 同一
    口径），**打不出分就按 +0 结算**，不让整轮回不了话。
    """
    gd = gameday if isinstance(gameday, dict) else {}
    plan = str(gd.get("plan") or "").strip()
    ctx = affection.scoring_ctx(line_key) or {"value": 0, "tier": "正常关系",
                                              "level": "隔得很远", "arousal": 0,
                                              "corrupt": 0, "submit": 0,
                                              "suspicion": 0,
                                              "recent": "（暂无记录）"}
    prompt = _SCORE_PROMPT % {
        "value": ctx["value"],
        "tier": ctx["tier"],
        "level": ctx.get("level") or "?",
        "arousal": int(ctx.get("arousal") or 0),
        "arousalName": ctx.get("arousalName") or "—",
        "excite": int(ctx.get("excite") or 0),
        "exciteName": ctx.get("exciteName") or "还没进场景",
        "dynamicBrief": ctx.get("dynamicBrief") or "（这一轮没有额外的动态规则）",
        "corrupt": int(ctx.get("corrupt") or 0),
        "submit": int(ctx.get("submit") or 0),
        # 戒备（2026-11-05）：模型要知道她现在起没起疑心，才知道同一句话
        # 该放行还是该往回打。缺字段的老 ctx 按 0 走。
        "suspicion": int(ctx.get("suspicion") or 0),
        # 开发度（2026-11-05）：编剧看到五处各到哪一档，才判得出要不要给勾子。
        # 缺字段的老 ctx 按「都还没开发过」走。
        "dev": ctx.get("devText") or "（五处都还没被开发过）",
        "recent": ctx["recent"],
        # 游戏内时间（2026-11-04）：打分 AI 顺带管时间推进，得知道今天第几天、
        # 有没有挂着没兑现的约定。
        "gday": _day_of(gd),
        # 当前时段（2026-11-07）：2026-11-07 的复测发现 phase 字段永远是空的——
        # 打分器只看得到「第几天」，看不到「现在是早晨」，它就没法判断「变了没有」。
        # 不给这一格，`gd["phase"]` 只能靠玩家手动点时间按钮才会动。
        "gphase": str(gd.get("phase") or "还没定"),
        "gplan": _plan_fact(gd, plan),
        "profile": _score_profile(card, state, key=line_key),
        "said": str(said or "")[:2000],
        "history": _judge_recent(history),
        "ledger": _judge_ledger(note),
        "_facts_guide": _FACTS_GUIDE,
    }
    try:
        # **不传 max_tokens**（2026-11-05 用户拍板：不要任何输出上限）。
        # 实测过三档：不传（服务端默认）706 字、1600 给 582 字、4000 给 615 字
        # ——根本不是长度限制的问题，加了反而更短。真正的原因在提示词里
        # 那句「原文照抄」：模型逐字复述露骨原文时会被内容审核当场掐断，
        # 末尾补一句 `Sorry, I can't respond`。改措辞就好了（见 `segments`）。
        raw = _llm_call([{"role": "user", "content": prompt}], timeout=LLM_JUDGE_TIMEOUT)
    except Exception:                            # noqa: BLE001
        log.warning("攻略模式打分调用失败，按 0 分走")
        return None, "", None
    d = _parse_json_loose(raw)
    if not isinstance(d, dict):
        # 打日志时给足上下文：前面是回复开头，后面是结尾——截断的现场在末尾。
        log.warning("攻略模式没交 JSON，按 0 分走（共 %d 字）：头=%r 尾=%r",
                    len(str(raw or "")), str(raw or "")[:120],
                    str(raw or "")[-120:])
        return None, "", None
    intent = str(d.get("intent") or "").strip().lower()
    if intent not in INTENTS:
        intent = None
    note = str(d.get("note") or "").strip()[:30]
    score = {"delta": d.get("delta"),
             "turn_type": d.get("turn_type") or "chat",
             "reason": d.get("reason") or "",
             # 怀孕（2026-11-05 / 2026-11-09 补回）：模型判的「射在里面」以前
             # 在这里被丢掉，只有「高潮满 → 弹窗」能记账；玩家正文里写明的
             # 内射全漏。`run_turn` 的 `_preg_record` 只认 `finish == "inside"`。
             "finish": _norm_finish(d.get("finish")),
             # 2026-11-04 距离层级三件事：这一刻的气氛、有没有越界、她答不答应、
             # 有没有做成一件能让关系往前走的事。**别在这里漏掉任何一项**——
             # 这个 dict 是 settle 唯一的输入，漏了就等于模型白判。
             "arousal": d.get("arousal"),
             "cross": d.get("cross"),
             "allow": d.get("allow"),
             "beat": d.get("beat"),
             # 2026-11-04 晚 兴奋度两件事：这轮在不在场景里、她的兴奋涨落多少。
             # 同样**不许漏**——settle 只认这个 dict。
             "in_scene": d.get("in_scene"),
             # 2026-11-05 用户改回：涨多少由这一轮的场景 AI 判（`excite` 就是
             # 增量本身，-40~+40），`act` 只判「有没有身体接触」，
             # orgasm_hints.base 退成「模型没给」时的兜底。
             "act": d.get("act"),
             "excite": d.get("excite"),
             # 堕落 / 屈服（2026-11-04 深夜）：这两根轴不进好感结算，
             # 只改她这一轮怎么演。`soothe` 是唯一能让堕落往下走的口子——
             # 不带上它，模型给的负值会被 `settle` 吞掉（见那边注释）。
             "corrupt": d.get("corrupt"),
             "submit": d.get("submit"),
             "soothe": d.get("soothe"),
             # 戒备（2026-11-05）：settle 只认这个 dict，漏了警戒轴就死在 0。
             "suspicion": d.get("suspicion"),
             # 开发度（2026-11-05）：`dev` 是五处增量、`spot` 是这一轮主碰的
             # 部位、`hook` 是身体先失控那一句。三个都**不许漏**——settle 只认
             # 这个 dict，漏了 developer 轴就永远停在 0，面板一行都不会动。
             "dev": d.get("dev"),
             "spot": d.get("spot"),
             "hook": d.get("hook"),
             # 游戏内时间（2026-11-04）：settle 不认识这几个键，会原样忽略；
             # run_turn 从同一个 dict 里取，落进会话的 gameday。
             "time": d.get("time"),
             "phase": d.get("phase"),
             "plan": d.get("plan")}
    # 导演条（2026-11-05）：分段 + 潜台词，第二段演员要拿它。
    # 放在 score 里一起返回，不另开接口——`settle` 不认识这些键，会原样忽略。
    score.update(_director_fields(d))
    # 诊断行（2026-11-05）：以前只看得到失败，成功的打分一次都不留痕，
    # 「数值为什么不涨」只能靠猜。这一行把模型实际给的关键字段摊出来，
    # 排查时 grep `[score]` 就行。
    log.info("[score] act=%r spot=%r excite=%r dev=%r Δ=%r",
             d.get("act"), d.get("spot"), d.get("excite"), d.get("dev"),
             d.get("delta"))
    return intent, note, score


# 意图 → 主提示词里那行注入。没有这一条对应项就什么都不加。
_INTENT_HINT = {
    "intimate": ("对方在求欢、要亲近。", "顺着接，可以进入亲密描写；"
                                     "尺度按他说到哪儿写到哪儿。"),
    "joke": ("对方在开玩笑、逗你，不是认真地要你去做这件事。",
             "**先听出这是玩笑**：可以回怼、翻个白眼、配合着演、或者假装生气。"
             "不要当真去办事（别真的讨论怎么做菜），也不要因为这句话就脸红心跳——"
             "玩笑归玩笑，害羞留给真正的亲近。"),
    "literal": ("对方在认真地要求你做事或问问题。", "正常应对，该做做、该答答。"),
    "insult": ("对方在贬低、羞辱你。", "会难过或者会生气，按角色性格反应，不要当没事。"),
    "neutral": ("", ""),
}


def _intent_block(intent, note):
    """把判断结果拼成系统头里的一段。intent 为空时不加。"""
    if not intent or intent not in _INTENT_HINT:
        return ""
    what, how = _INTENT_HINT[intent]
    if not what:
        return ""
    lines = ["## 他刚才在干什么", "", what]
    if note:
        lines.append("（系统判断：%s）" % note)
    lines.append("")
    lines.append(how)
    return "\n".join(lines)


def _director_block(info, plot=""):
    """把编剧交下来的**事实**拼成系统头里的一小段。没有内容就不加。

    2026-11-09 收窄：这里以前还拼潜台词、她察觉到的、这一拍的态度、主动的
    勾子——那四样都是「这一拍该怎么写」的判断，编剧替写作层做完之后，写作
    只剩照着演，正文准但没有意外。现在只剩两样：

      - `secret`：他有没有藏着话（事实，不是写法）；
      - `plot`：玩家自己在「剧情推进」栏写的方向（他给的，不是编剧写的）。

    `info` 是 `_director_fields()` 的返回，或者攻略模式那个 score dict。
    """
    if not isinstance(info, dict):
        return ""
    secret = bool(info.get("secret"))
    # 玩家写的「剧情推进」本来就是给写作层的话，直接透传，方向由模型写。
    direction = re.sub(r"\s+", " ", str(plot or "").strip())[:200]
    if not (secret or direction):
        return ""
    lines = ["## 这一拍你怎么读对方", ""]
    if secret:
        lines.append("- 对方心里有话没说出口，内容你不知道，只能从表现上猜。")
    if direction:
        lines.append("- 这一拍往这个方向走，让它真的发生在画面里：%s" % direction)
    lines.append("")
    lines.append("这些是给你看的提示，**不是台词**。不要引用、不要点破，"
                 "也不要替对方把话说破。")
    return "\n".join(lines)



# ─── 游戏内时间（2026-11-04）────────────────────────────
#
# 用户诊断：70 轮对话像一天——「时间」这个维度不存在，玩起来没有推进感。
# 方案（已对齐）：代码只记「天」，**时段和跨天由打分 AI 顺带判**（它每轮
# 都在读剧情，最有资格说「这一天过完了没有」），零新增 LLM 调用。
# 好感结算器已经不按现实日历记账（按天额度 11-04 晚全取消），这里的时间
# 是纯剧情时间，跟现实时钟、跟 streak 都不相干。

# 同一天聊过这个轮数，就往注入里塞一句「天色不早了」——兜底用，
# 正常的过夜应该由打分 AI 的 time=overnight 触发。
def _apply_gameday(key, score=None, force_new_day=False, days=1):
    """把这一轮的时间推进落账，返回最新 gameday（会话不存在返回 None）。

    force_new_day（「睡到明天」/时间跳跃）：不看打分，直接 +days 天并掷事件。
    跳多天时不掷日常小事件——「台风天」这种一句话用在一个月跨度上很怪。
    """
    try:
        days = max(1, min(365, int(days or 1)))
    except (TypeError, ValueError):
        days = 1
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return None
    gd = _gameday(ent)
    # 回退快照（2026-11-04）：这轮把时间推进之前先留一份原样。「回退上一轮」
    # 时整个换回去——不然撤掉一条「说好明天见」的消息，游戏日却停在了第 2 天。
    # 只保一级：每次覆盖上一份（deepcopy 里不会嵌 gameday_prev，它挂在 ent 上）。
    ent["gameday_prev"] = copy.deepcopy(gd)
    # 怀孕状态跟着天数一起存快照（2026-11-05）：跨天那一轮可能刚掷中，
    # 「回退上一轮」只退天数不退怀孕的话，回退完她还怀着。
    ent["preg_prev"] = copy.deepcopy(ent.get("preg"))
    gd["turns_today"] = int(gd.get("turns_today") or 0) + 1
    if force_new_day:
        gd["day"] = int(gd["day"]) + days
        gd["phase"] = "早晨"
        gd["event"] = _roll_event(gd) if days == 1 else ""
        gd["turns_today"] = 0
        # 跳了好几天，挂着的约定多半已经过期（2026-11-06）：原来只在
        # 「正好跳到那一天」时销账，跳 7 天就把它落地成了「今天的事件」，
        # 于是人都到目的地了，模型还在按「前往某地」写行程。
        pday = int(gd.get("plan_day") or 0)
        if gd.get("plan") and pday and pday <= gd["day"]:
            gd["plan"] = ""
            gd["plan_day"] = 0
    elif isinstance(score, dict):
        t = str(score.get("time") or "none").strip().lower()
        if t in ("overnight", "skip_days"):
            gd["day"] = int(gd["day"]) + (3 if t == "skip_days" else 1)
            gd["phase"] = normalize_phase(score.get("phase"))[:12] or gd["phase"]
            gd["event"] = _roll_event(gd)
            gd["turns_today"] = 0
        else:
            # 2026-11-07 起第一段**不再要求**输出 phase（角色 AI 的状态行第四段
            # 是主来源）。这里保留读取只为两条路都通：旧版模型偶然还在给，
            # 给了就采纳；不给就保持状态行给的值，不会被写成空。
            ph = normalize_phase(score.get("phase"))
            if ph:
                gd["phase"] = ph[:12]
        # 2026-11-10：约定在**同一天**约、同一天做完时，跨天销账根本不触发，
        # 旧 plan 会一直挂着被角色当成还欠着提。裁判现在把 plan 写成「到这一拍
        # 仍然有效」的约定：做完了就交空串销账。代码只在它**确实交了字符串**
        # 时覆盖（含空串）；没交作业（None）就沿用，不替它猜。
        raw_plan = score.get("plan")
        if isinstance(raw_plan, str):
            plan = raw_plan.strip()
            gd["plan"] = plan[:80]
            gd["plan_day"] = int(gd["day"]) + 1 if plan else 0
        # 换地方了就更新（2026-11-06）：留空表示「还在原处」，别每轮都写。
        place = str(score.get("place") or "").strip()
        if place:
            gd["place"] = place[:40]
    _save_state(st)
    return gd


def game_day(session_key):
    """这条线游戏内第几天（面板/回包用）；没在演返回 None。"""
    ent = _entry(resolve_line(session_key))
    if not isinstance(ent, dict):
        return None
    return _gameday(ent).get("day")


# 时段常量与 normalizing 见 game_time。


def game_phase(session_key):
    """这条线当前时段；没在演返回 ""。"""
    ent = _entry(resolve_line(session_key))
    if not isinstance(ent, dict):
        return ""
    return str(_gameday(ent).get("phase") or "")


def set_phase(session_key, phase, sender_uin=""):
    """手动切换今天时段，返回 `{"day": N, "phase": "中午"}`；没在演返回 None。

    刻意不碰 `gameday_prev`：切时段不是一轮对话，回退上一轮不该把它撤掉。
    也不重置 `turns_today`，否则「这一天聊了很久」的收束节拍会被刷掉。
    """
    ph = normalize_phase(phase)
    if ph not in PHASES:
        return None
    key = resolve_line(_persona_key(session_key, sender_uin))
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return None
    gd = _gameday(ent)
    gd["phase"] = ph
    _save_state(st)
    return {"day": gd["day"], "phase": gd["phase"]}


# ─── 怀孕 / 时间跳跃（2026-11-05）────────────────────────
#
# 判定只在**跨天**发生：点时间跳跃，或编剧判出 time=overnight / skip_days。
# 普通聊到晚上不判，避免同一场戏里反复掷骰。

def _preg_record(key, card, day, finish):
    """这一轮编剧判了 inside，就记在当天；不立刻判定。

    记录是**只增不清**的：同一天后面几轮判 none / outside 不会抹掉它，
    只有跨天结算时才统一清账。这样「今天射进去过」不会被编剧后面给出的
    新判断覆盖。
    """
    if str(finish or "").strip().lower() != "inside":
        return
    if not pregnancy.card_enabled(card):
        return
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return
    # 回退快照（2026-11-05）：存「记录之前」的状态；回退上一轮时连这一轮
    # 的内射记录一起撤掉，不会出现「消息退了、精子还在」。
    before = copy.deepcopy(ent.get("preg"))
    if pregnancy.record_inside(ent, day):
        ent["preg_prev"] = before
        _save_state(st)


def _preg_advance(key, old_day, new_day):
    """跨天结算，返回 conceived / birth / None。"""
    st = _load_state()
    ent = st.get(key)
    if not isinstance(ent, dict):
        return None
    event = pregnancy.advance(ent, old_day, new_day)
    _save_state(st)
    return event


def pregnancy_status(session_key):
    """给前端状态面板用的怀孕/孩子状态。"""
    key = resolve_line(_persona_key(session_key, ""))
    ent = _entry(key) or {}
    card = active_card(key)
    day = _gameday(ent).get("day") if ent else 1
    return pregnancy.view(ent, day,
                          enabled=bool(card and pregnancy.card_enabled(card)))


def pregnancy_block(session_key, event=None):
    """拼进系统头/跳跃任务的身体状态；卡没开怀孕就返回空串。"""
    key = resolve_line(_persona_key(session_key, ""))
    ent = _entry(key) or {}
    card = active_card(key)
    if not card or not pregnancy.card_enabled(card):
        return ""
    day = _gameday(ent).get("day") if ent else 1
    return pregnancy.prompt_block(ent, day, event)


# 女生主动表白（2026-11-08）：本轮结算后表白值到满、感情线到「认准你了」，
# 第一段已经是正常回复，这里再让演员补一段，把攒了很久的那句话说出口。
# 两段之间不换场景、不跳时间，也不收场——角色扮演没有结尾。


def _confession_reply(head, model_hist, first_reply, card=None, user_block=""):
    """主动表白的第二段。失败就返回空串，第一段照常发出去。

    `user_block`（2026-11-09 修）：玩家**这一轮**说的话必须一起带上。
    原先只传了历史，当前这句不在 messages 里，第二段只能看见自己刚才的回
    复——她说出口的那句话接的是「上一轮」，不是玩家刚说的这句。带上之后
    消息序列和第一段完全同构（head + 历史 + 这一轮玩家 → 她的回复 → 任务），
    前缀缓存也照样命中。
    """
    msgs = [{"role": "system", "content": head}] + list(model_hist or [])
    if str(user_block or "").strip():
        msgs.append({"role": "user", "content": user_block})
    msgs += [{"role": "assistant", "content": first_reply},
             {"role": "user", "content": _AUTO_CONFESS_TASK}]
    try:
        raw = _llm_call(msgs, temperature=TEMPERATURE)
    except Exception:                            # noqa: BLE001
        log.exception("主动表白第二段调用失败")
        return ""
    try:
        out = split_turn(raw, default_mood="")[0]
    except Exception:                            # noqa: BLE001
        out = str(raw or "").strip()
    return str(out or "").strip()


def pop_item_fade(session_key):
    """取「这一轮刚失效的道具」并**立刻清掉**（读一次就够，下一轮不再显示）。

    和 `take_pending_finish` 同一个套路：状态是给前端看一眼的，
    留在存档里下次刷新会重复画那条删除线。
    """
    try:
        key = resolve_line(session_key)
        st = _load_state()
        rec = st.get(key) or {}
        fade = rec.pop("item_fade", None)
        if not isinstance(fade, dict) or not fade.get("name"):
            return None
        st[key] = rec
        _save_state(st)
        return {"key": str(fade.get("key") or ""), "name": str(fade.get("name") or "")}
    except Exception:                                     # noqa: BLE001
        log.warning("取道具失效标记失败 %s", session_key, exc_info=True)
        return None


def run_turn(session_key, text, target, sender="", sender_uin="",
             manual=None, writing=None):
    """扮演态的一轮：**两次** LLM 调用，返回 `Turn(正文, 画面, 情绪, 强度, 状态)`。

    `writing`（2026-11-06）：`{mode, note}`，酒馆那套分层写法的开关。`mode`
    是文风档位（进 system 头），`note` 是玩家给的「这一幕的调子」（进贴脸
    尾注）。**不传或全空 = 不拼档位、不加尾注**，提示词其余部分照旧。

    `manual`（2026-11-06）：网页「精确输入」的三栏
    `{thought, action, speech, plot}`。填了就以它为准，编剧 AI 不用再拆主
    输入框；`thought` 只给编剧，绝不给演员。

    2026-11-02 起是两段式：
      1. `_judge_intent()` —— 短提示词、不带人设，判断这句话在干什么；
      2. 主调用 —— 拿着判断结果写正文。
    第一段失败（超时、没交 JSON、类别不认识）就退化成原来的单次流程，
    只是少了那一行注入，不影响这一轮能回话。

    正文空 = 这轮什么都不发（调用方别开口）；画面空 = 这轮不出图。

    `sender_uin`（2026-11-02）：群里说话人的 QQ 号。**群聊按人隔离状态**，
    见 `_persona_key`——不隔离的话 A 刷的好感会算到 B 头上。私聊传不传都行。
    """
    key = _persona_key(session_key, sender_uin)
    # 写作档位只做归一化：非法档位退回常态，作者注压成单行。判断写什么
    # 交给模型，这里不设准入闸门。
    mode, author_note = writing_style.norm(writing)
    styled = mode != writing_style.DEFAULT_MODE or bool(author_note)
    # QQ 私聊分线（2026-11-03 方案 B）：base → 当前角色线。网页传来的已是
    # 线键，resolve 幂等。历史/小记/好感从此按角色各存各的。
    key = resolve_line(key)

    # 玩家直接说话了 = 那个「射在哪里」的弹窗被跳过了（2026-11-06）。
    # 不清的话，刷新页面还会把上一轮的弹窗再弹一次。
    _set_pending_finish(key, None)

    # ── 群聊不演（2026-11-03 用户反馈）──────────────────────────
    #
    # 用户发现「群里其他人能看到别人的聊天内容」。状态隔离一直是好的，
    # 问题在**群聊内容本来就公开**——机器人发的消息全群可见，隔离记忆
    # 没用。所以群聊这条彻底关掉实际扮演。
    #
    # 返回空的 Turn：调用方不会发任何东西（`_run_persona_turn` 里
    # `turn.reply.strip()` 为空就不发）。群里想玩的人会先收到
    # `handle_command` 的引导语，不会莫名其妙等一个没反应。
    #
    # 存量的 `group_<群号>_<QQ号>` 扮演态自然失效——那条线再也不会被激活。
    if str(session_key or "").startswith("group_"):
        log.info("群聊不扮演，已忽略：%s", key)
        return Turn("", "")

    card = active_card(key)
    if card is None:
        return Turn("", "")

    # 长期记忆 RAG（2026-11-05）：小模型 CPU 冷启动十几秒，只在后台拉起来。
    # 这一轮如果还没 ready，就照常聊，不带回忆；下一轮开始生效。
    # 游客只玩 30 轮，不存也不查（见 `persona_rag.enabled_for`）——不白占资源。
    rag_on = persona_rag.enabled_for(key)
    if rag_on:
        persona_rag.warm()
        # 老线原文补齐（2026-11-10）：原文层上线前的轮次只有提炼摘要，
        # 旧条目还被 400 字截过。本进程每条线只排一次后台回填，幂等；
        # 全量归档用 callable 传，没排上就不读，不拖聊天这一轮。
        try:
            persona_rag.schedule_backfill(key, lambda: load_archive(key))
        except Exception:                                    # noqa: BLE001
            log.exception("原文回填排程失败 %s", key)

    ent = _entry(key) or {}
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")
    # 当前穿着（2026-11-09）：独立固化的英文串，本轮要作为事实递给演员。
    # 首轮还没有，就让它按角色卡自己定一身，这一轮交回来后开始固化。
    prev_outfit = str(ent.get("outfit") or "").strip()
    gd = _gameday(ent) if ent else None
    day_before = _day_of(gd)

    # 历史先读出来（2026-11-05）：第一段从「打分器」升级成「整理器 + 编剧」，
    # 它也要看最近发生过什么，才能判潜台词。原来它只看当前这一句。
    raw_hist = _load_hist(key)
    hist = trim(raw_hist)

    # 精确输入（2026-11-06）：玩家分栏标注时，分段以玩家为准，编剧只负责
    # 判意图/打分/导演条。心理活动放进编剧输入，但不进演员输入。
    man = _manual_norm(manual)
    man_segs = _manual_segments(man)
    man_block = _manual_block(man)
    judge_said = text
    if man_block:
        judge_said = man_block + (
            ("\n\n【主输入框（补充）】\n" + text) if text else "")

    # 第一段：这句话在干什么。**用原始文本判**，不带「张三：」这种前缀。
    # 攻略模式（2026-11-03）：同一次调用顺带给关系打分，不多烧一轮。
    raid = affection.is_raid(key)
    # v2 正向关系线：新档走独立打分器和独立结算，老档一条代码都不换。
    v2 = raid and affection.is_v2(key)
    extras = {}
    score = None
    if raid:
        if v2:
            intent, inote, score = _judge_v2(
                card, judge_said, prev_state, line_key=key, gameday=gd,
                history=hist, note=note)
        else:
            intent, inote, score = _judge_intent_scored(
                card, judge_said, prev_state, line_key=key, gameday=gd,
                history=hist, note=note)
        extras = score if isinstance(score, dict) else {}
    else:
        intent, inote, extras = _judge_intent(
            card, judge_said, prev_state, history=hist, note=note)
        extras = extras if isinstance(extras, dict) else {}
    # 精确输入（2026-11-09）：玩家亲手填的三栏是他本人的意图，照旧按块发；
    # 编剧那边已经不拆段了（它只记数值和事实），所以这里只剩 man_segs 一个来源。
    # 有心理活动就锁 secret——内容给写作层，但不许她"听见"。
    if man_segs:
        extras["addressed"] = True
        extras["secret"] = bool(man.get("thought"))
    finish = _norm_finish(extras.get("finish"))
    # 射精选择（2026-11-05）：上一轮她高潮满了，玩家在弹窗里选了「射在哪里」。
    # 这个选择**不是**玩家在正文里写的，所以编剧判不出来，得由系统补进去。
    # 取一次就清，免得后面每一轮都在射。
    fin_pick = take_finish(key)
    if fin_pick:
        # 位置决定「有没有射在里面」——怀孕记账只认这个，不能靠编剧猜。
        if fin_pick in ("pussy", "anus"):
            finish = "inside"
        elif fin_pick in ("mouth", "body"):
            finish = "outside"
        else:
            finish = "none"
    # 亲密场景那 1,284 字只在该用的时候拼（2026-11-03）。
    # 判不出来（intent 为空）时按 True 走——宁可多发，不要因为分类失败让
    # 亲密场景写得干巴巴。`neutral` 也带上，它随时可能拐到亲密去。
    intimate = intent in (None, "", "intimate", "neutral")
    head = build_head(card, session_key, sender, note, prev_state,
                      intimate=intimate, mode=mode, dynamic=False)
    # 缓存 Phase B：逐轮恒定的写法说明并进 system 头（稳定前缀）吃命中，
    # 末尾玩家消息里那份由 _strip_stable_notes 剔除，文字总量不变。
    head = head + "\n\n" + "\n\n".join(_stable_note_blocks())
    # 缓存优化 Phase A（2026-11-10）：所有逐轮变化的内容不再进 system 头，
    # 改攒进 `dyn_parts`，最后统一贴到末尾玩家消息，保住 head 的前缀缓存。
    dyn_parts = []
    nb = _note_block(note)
    if nb:
        dyn_parts.append(nb)
    sb = _state_block(prev_state)
    if sb:
        dyn_parts.append(sb)
    block = _intent_block(intent, inote)
    if block:
        dyn_parts.append(block)
    # 导演条（2026-11-05）：第一段顺手判的潜台词 / 她察觉到的 / 这一拍的态度。
    # 它只给演员「怎么反应」，不给台词——所以放在 intent 之后、结算之前。
    dblock = _director_block(extras, plot=man.get("plot"))
    if dblock:
        dyn_parts.append(dblock)
    # 射精位置（2026-11-05）：玩家在弹窗里选的那个，不由正文决定。
    # 拼在导演条之后、结算之前——它改的是「这一拍怎么写」，不是数值。
    fblock = finish_block(fin_pick)
    if fblock:
        dyn_parts.append(fblock)
    # 长期记忆（2026-11-05）：按玩家这一轮**看得见的话**去翻旧账。
    # 用户没说出口的「想」不参与检索——拿心里话去翻回忆，容易让她提前知道。
    #
    # 2026-11-07：这里只**取**不拼。回忆块改成和「故事到现在」一起放到
    # 最后一条玩家消息里——system 头是前缀，动态内容插进去会把前缀缓存
    # 打散（实测命中 59~70%，一轮两万 token 时这一步很贵），放末尾既保住
    # 缓存，又离生成点更近。
    visible = _visible_user_text(
        man_segs, "" if man.get("thought") else text)
    rblock = ""
    if rag_on:
        try:
            rblock = persona_rag.build_block(
                key, visible, day=day_before, context=_rag_context(hist))
        except Exception:                                # noqa: BLE001
            log.exception("回忆注入失败 %s", key)
    # 攻略模式结算（2026-11-03）：分数落账 → 按档位往主提示词里注入关系块。
    # 没打出分（score 为 None）也照走 settle——按 +0 记账，档位注入照常。
    preg_event = None
    aff_res = None
    if raid:
        aff_res = affection.settle(key, score, game_day=day_before)
        # v2 的 res 是另一套轴，没有高潮/内射/怀孕那几项，别拿 v1 后处理去读。
        if isinstance(aff_res, dict) and not v2:
            # 让状态块知道这一轮有没有内射：高潮和内射同一拍时换写法。
            aff_res["finish"] = finish
            # 怀孕/产后阶段（2026-11-06）：开发度满格时要靠它判断
            # 「一碰就出水 / 一含就冒汁」该不该加码。
            try:
                aff_res["pregStage"] = pregnancy.stage_key(
                    _entry(key) or {}, _gameday(_entry(key) or {}).get("day"))
            except Exception:                        # noqa: BLE001
                aff_res["pregStage"] = ""
            # 高潮满了要弹窗（2026-11-05）：前端看到 `climax` 才问
            # 「射在哪里」。`last=True` 表示这已经是第 5 次——AI 回完之后
            # 还要再问一次「结束 / 最后一次」。
            cl = _climax_info(aff_res)
            if cl:
                aff_res["climax"] = cl
                # 落盘成待办：页面刷新/断网后还能再问一次（见
                # `_set_pending_finish` 的说明）。
                _set_pending_finish(key, cl)
        # 道具（2026-11-08）：记录进出的东西 + 拼注入块。
        # 三块拼在一起：能用的清单（常态）、这一轮用到的写法、她给过的东西。
        try:
            _istate = _load_state()
            _irec = _istate.get(key) or {}
            # 用 extras 不是 score：自由模式下 score 是 None，但 extras 三种
            # 情况都有值（v2 / v1攻略 / 自由），道具在三条路上都要能用。
            _sc = extras if isinstance(extras, dict) else {}
            _idelta = _sc.get("items") if isinstance(_sc.get("items"), dict) else {}
            # 编剧没给就自己从原话里认一遍（只认表里的别名，不猜）
            _use = [str(x) for x in (_idelta.get("use") or []) if str(x).strip()]
            if not _use:
                _use = _items.match(text)
            _gains = _idelta.get("gain") or []
            if _gains:
                _irec["items"] = _items.apply_gain(_irec.get("items"), _gains)
                _istate[key] = _irec
                _save_state(_istate)
            # 生效中的道具（2026-11-08）：一次只一个。倒计时由代码做，
            # 不靠模型记「第几轮」——它记不住。
            _act = _items.normalize_active(_irec.get("active_item"))
            _fade_name, _fade_key = "", ""
            if _act:
                _act, _fade_name = _items.tick(_act)
                _fade_key = str((_irec.get("active_item") or {}).get("key") or "")
            _started = ""
            if _use:
                # 一次只用一样：多个就用第一个，其余忽略（用户拍板，先做简单版）
                _act, _old = _items.start(_act, _use[0], turn_total(key))
                if _old:
                    log.info("道具顶替：%s 被 %s 换下", _old, _use[0])
                _started = _use[0]
            # 写回存档（active 变了就存）
            if _act or _fade_name or _irec.get("active_item"):
                _irec["active_item"] = _act
                # 失效那一轮留个标记给前端画删除线，读一次就清（见 pop_item_fade）
                if _fade_name:
                    _irec["item_fade"] = {"key": _fade_key, "name": _fade_name}
                _istate[key] = _irec
                _save_state(_istate)
            for _blk in (_items.list_block(),
                         _items.use_block([_started] if _started else []),
                         _items.active_block(_act, just_started=bool(_started)),
                         _items.fade_block(_fade_name, _fade_key),
                         _items.keepsake_block(_irec.get("items"))):
                if _blk:
                    dyn_parts.append(_blk)
        except Exception:                                # noqa: BLE001
            log.warning("道具注入失败 %s", key, exc_info=True)
        # v2 成人向（2026-11-08）：编剧判好的 adult delta → 结算 → 拼中文注入。
        # 老档（没 schema=2）不会进这个分支，v1 一个字不动。
        if v2 and isinstance(score, dict) and isinstance(score.get("adult"), dict):
            try:
                _astate = _load_state()                  # 读整份，改一条，写回
                _rec = _astate.get(key) or {}
                _ast = adult_state.load(_rec)
                _n_before = int(_ast.get("orgasm_count") or 0)
                _ast, _anotes = adult_state.apply(
                    _ast, score["adult"],
                    value=(aff_res or {}).get("value") if isinstance(aff_res, dict) else None)
                # 只写 affection 下面那一份（2026-11-08）：adult 状态属于
                # 关系档的一部分，和 value/contacts 放在一起，别在顶层再存一份
                # ——两份会各自被读，一旦不同步就是「显示的和我看到的不一样」。
                adult_state.save(_rec, _ast)     # 写 affection.adult，顺手清顶层那份
                _astate[key] = _rec
                _save_state(_astate)
                # 高潮弹窗（2026-11-09 补 v2）：v2 以前没有 `just_orgasm`，
                # 「射在哪里」永远不弹，怀孕记账也就没有来源。这里用
                # `orgasm_count` 的增量当唯一事实，和 v1 的 `_climax_info` 同口径。
                _n_now = int(_ast.get("orgasm_count") or 0)
                if _n_now > _n_before and isinstance(aff_res, dict):
                    aff_res["just_orgasm"] = _n_now
                    aff_res["orgasmMax"] = affection.ORGASM_MAX
                    _cl = _climax_info(aff_res)
                    if _cl:
                        aff_res["climax"] = _cl
                        _set_pending_finish(key, _cl)
                for _n in _anotes:
                    log.info("adult %s：%s", key, _n)
            except Exception:                            # noqa: BLE001
                log.exception("成人向结算失败 %s", key)
            else:
                _ab = adult_state.block(_ast)
                if _ab:
                    dyn_parts.append(_ab)
        blk = affection.injection_block(aff_res)
        if blk:
            dyn_parts.append(blk)
        # 动态状态（2026-11-06 用户要求）：冲动/高潮是这一轮的气氛，档位到了
        # 就临时注入一段行为规则——她这一拍会主动到什么程度、脸上什么样。
        # 和长期轴（好感/堕落/屈服）分开：只在这一轮有效。
        dyn = affection.dynamic_block(aff_res)
        if dyn:
            dyn_parts.append(dyn)
        # 游戏内时间（2026-11-04）：把打分 AI 判的跨天/约定落账，
        # 再把「第 N 天 + 今天的事 + 约定」拼进系统头。
        #
        # **2026-11-05 取消「第五次自动跨天」**（用户拍板）：那一步改成弹窗
        # 驱动的两段——第五次先正常输出，AI 回完再问「结束 / 最后一次」。
        # 自动跳会让玩家没得选，而且第五次那一轮被硬塞了「过渡到第二天」的
        # 指令，写出来就不再是那一场本身了。
        gd = _apply_gameday(key, score) or gd
        # 跨天过渡（2026-11-10）：代码只报「上一拍收在第几天」这个事实，
        # 怎么把昨天接过来由模型自己写。自由模式不推进天数，恒为 False。
        crossed_day = int((gd or {}).get("day") or day_before) > day_before
        # 怀孕记录（2026-11-05）：放在时间推进/快照**之后**写。`preg_prev`
        # 必须是这一轮开始前的状态，回退时才能把本轮的内射记录一起撤掉。
        if finish == "inside":
            try:
                _preg_record(key, card, day_before, finish)
            except Exception:                            # noqa: BLE001
                log.exception("怀孕记录失败 %s", key)
        try:
            new_day = int((gd or {}).get("day") or day_before)
            if new_day > day_before:
                # 先清 v2 的高潮值/冷却：怀孕结算万一抛异常，收场也不该漏。
                _clear_adult_night(key)
                preg_event = _preg_advance(key, day_before, new_day)
        except Exception:                                # noqa: BLE001
            log.exception("怀孕结算失败 %s", key)
        tblock = _gameday_block(gd, new_day=crossed_day)
        if tblock:
            dyn_parts.append(tblock)
    else:
        # 自由模式不推进天数，但内射照样按天记账，等时间跳跃时统一结算。
        if finish == "inside":
            try:
                _preg_record(key, card, day_before, finish)
            except Exception:                            # noqa: BLE001
                log.exception("怀孕记录失败 %s", key)
        # 手动切的时段也要进系统头（2026-11-05）：否则自由模式切到晚上，
        # 她的下一句还按白天的场景写。
        tblock = _gameday_block(gd, new_day=False)
        if tblock:
            dyn_parts.append(tblock)
    # 怀孕/孩子（2026-11-05）：身体状态每轮都该在她脑子里，不需要等玩家问。
    # 必须放在跨天结算**之后**拼：刚发现怀孕 / 刚生产这一轮就要按新状态演，
    # 不能等下一轮才让她想起来（用户说的「先结算，再开场」）。
    pblock = pregnancy_block(key, preg_event)
    if pblock:
        dyn_parts.append(pblock)
    # 压迫模式（2026-11-05）：在 `aff_res` 上补一个 `forced` 标记。
    # **必须在这里补，不能指望 `injection_block` 写回**——它是纯函数，
    # 拿结果拼完就丢；这里不补的话，下面 `intimate_guide` 里的压迫层
    # 和 `writing_skills` 的 forced 轴永远进不去，整套玩法等于没接。
    if isinstance(aff_res, dict) and aff_res.get("active") and not v2:
        aff_res["forced"] = affection.is_forced(
            aff_res.get("value"), aff_res.get("corrupt"),
            aff_res.get("arousal"), aff_res.get("excite"))
    # 分层写法（2026-11-04 深夜）：按**这一轮的高潮值**挑一块「这一拍该怎么写」
    # 拼上去。放在 settle 之后是因为要拿最新状态——放在 build_head 里只能拿到
    # 上一轮的值，永远慢一拍。只在判成亲密时拼：`joke` / `literal` / `insult`
    # 那些场合用不上「到了怎么写」，拼进去只会分走注意力。
    if intimate and not v2:
        extra = intimate_guide(aff_res)
        if extra:
            dyn_parts.append(extra)
    # 数值读数（2026-11-09 用户拍板）：**每轮都给**，不再只挂在亲密轮上。
    #
    # 以前 `body_state_block` 写在上面那个 `intimate` 分支里，聊天轮她手里一个
    # 数都没有，只能靠编剧那句二手判断演；而 v2 那条线整段被 `not v2` 挡掉，
    # 写作层连「现在第几档」都看不到，全靠编剧的摘要。现在关系和数值每轮都给：
    # 她自己得知道现在站在哪儿，才谈得上「按区间演」。
    #
    # 位置保持在写法层之后（原来的顺序）：先把这一拍写到什么程度定下来，
    # 再谈用词，免得数值被写法层的词句盖住。
    if isinstance(aff_res, dict) and aff_res.get("active") and not v2:
        body = body_state_block(aff_res, gd)
        if body:
            dyn_parts.append(body)

    # 第一轮把网页上那段开场导读也发给模型（2026-11-04）。
    # 否则玩家照着导读里的场景开口（「咖啡好香」），模型手里根本没这个场景，
    # 只能另起一个头，两边当场对不上。只在没有历史时拼，之后靠状态行承接。
    if not hist:
        intro = str((_entry(key) or {}).get("intro") or "").strip()
        if intro:
            dyn_parts.append("【此刻】\n" + intro
                             + "\n（这是场景设定，照着它写，不要把这段话复述出来。）")

    # 群里带上说话人，模型才知道是谁在说话（同 qq_bot 正文前缀的做法）；
    # 私聊和空正文都不加——「张三：」后面什么都没有比不写更糊涂。
    said = text
    if target == "group" and sender and text:
        said = sender + "：" + text

    # 分段渲染（2026-11-09 改写）：**只有玩家自己标注的分栏**才按块发。
    #
    # 编剧那一层已经不再拆「说 / 做 / 想」了（它只记数值和事实）：它没有写作
    # 契约，硬拆经常拆错——把「他把杯子推过去」记成一句台词，写作 AI 照着错的
    # 分段写，动作就丢了。写作 AI 手里有整份契约和足够长的上下文，读原话比读
    # 别人的摘要准。玩家在「精确输入」里亲手填的三栏是**他本人的意图**，保留。
    segments = []
    facts = extras.get("facts") if isinstance(extras, dict) else []
    addressed = extras.get("addressed", True) if isinstance(extras, dict) else True
    user_fallback = said
    if man_segs:
        segments = man_segs
        addressed = True
        # 填了心理活动 → 主输入框只给编剧，不给她（防止顺手写在主框里的
        # 心里话漏出去）。看得见的内容请填动作/台词/剧情栏。
        user_fallback = "" if man.get("thought") else said
    user_block = render_user_block(segments, addressed, fallback=user_fallback,
                                   remind=False)
    # 近因位（2026-11-07）：所有组装路径统一用 `writing_style.tail()`，把最
    # 要紧的一句放在历史之后、生成之前。并进最后一条玩家消息，消息角色序列
    # 不变（不插中途 system，免得打断历史里带三行元数据的格式 few-shot）。
    # 常态只给一句正向锚点，替换原来那句人称禁令；玩家选了档位或写了调子
    # 才多带轮次和「这一幕」。
    #
    # 顺序是「原话 → 回忆 → 发展线 → 锚点」：越靠后越贴近生成点。发展线比
    # 回忆更近，因为用户抱怨的正是「不知道发展线路」；锚点（人称/篇幅/剧情
    # 推进）永远最后一句。
    if rblock:
        user_block = user_block + "\n\n" + rblock
    story = story_line(gd, note)
    if story:
        user_block = user_block + "\n\n" + story
    turn_no = None
    if styled:
        try:
            turn_no = turn_total(key) + 1
        except Exception:
            turn_no = 1
    # 上一轮的开头（反重复用，见 `writing_style.tail` 的 `last_open`）
    last_open = ""
    for _m in reversed(hist or []):
        if _m.get("role") == "assistant":
            _body, _ = _split_meta(str(_m.get("content") or ""))
            last_open = " ".join(_body.split())[:24]
            break
    # 场景义务（2026-11-08）：以前只在「多行 / 三栏分段」时才拼，单行输入
    # （最常见）整段没生效——玩家写「结完账我提起袋子」，模型直接跳到她的
    # 反应，缺了打包提袋那几步。每轮都给一次；已拼过的路径不重复。
    # 放在 tail 之前：它管这一拍怎么落地，tail 仍是最后一句。
    if _SCENE_DUTY not in user_block:
        user_block = user_block + "\n\n" + _SCENE_DUTY
    # 本轮事实点清单（2026-11-08）：放在场景义务之后、尾注之前，是演员
    # 生成前读到的最后一块实质内容。第一段没给 facts 就整块不出，行为不变。
    fb = _facts_block(facts)
    if fb:
        user_block = user_block + "\n\n" + fb
    # 当前穿着（2026-11-09）：和 facts 同层的事实，放在尾注之前。服饰只靠
    # 滑动窗口里上一轮那三行太弱，脱了外套下一轮就被模型重新穿回来。
    ob = _outfit_block(prev_outfit)
    if ob:
        user_block = user_block + "\n\n" + ob
    # 原地打转的刻度（2026-11-09）：同一地点连写 ≥3 拍才出现，只报事实。
    pb = _place_block(gd)
    if pb:
        user_block = user_block + "\n\n" + pb
    # 缓存优化 Phase A（2026-11-10）：把这一轮所有逐轮变化的块（导演条 /
    # 关系块 / 时间 / 怀孕 / 小记 / 状态 / 写法层 / 身体读数 / 开场导读…）统一
    # 贴到末尾玩家消息，与回忆/故事/事实/穿着同层、紧贴生成点。system 头因此
    # 只剩逐字节稳定的前缀，DeepSeek 前缀缓存才能整段命中。
    dyn_block = "\n\n".join(p for p in dyn_parts if p)
    if dyn_block:
        user_block = user_block + "\n\n" + dyn_block
    # 近因位再钉一次原话（2026-11-10）：写了心里话时主输入框本来就不给她，
    # 这时 user_fallback 为空，跳过——分段标签已经把可见动作说清了。
    echo_text = str(user_fallback or "").strip()
    if echo_text:
        user_block = user_block + "\n\n" + _THIS_TURN_ECHO + echo_text
    user_block = user_block + "\n\n" + writing_style.tail(
        turn_no, author_note, plot=man.get("plot"),
        name=char_name(card, key=key), mode=mode,
        last_open=last_open)
    # 恒定块已在 system 头里，这里去掉重复的一份：它们本来每轮都按 miss 付。
    # tail 仍是最后一句，原话仍在最前，动态块的相对顺序一点没动。
    user_block = _strip_stable_notes(user_block)
    # 前缀缓存滞回窗口（2026-11-10）：送模型的是**整段窗口**，不是 trim 的
    # 12 条。窗口起点逐轮不动，system 头 + 历史才能一整段命中；`trim` 只用来
    # 做单条截断（limit=窗口实际长度），不砍条数。编剧 / 打分器上面用的
    # `hist` 仍是最近 12 条，行为一点没变。
    win = cache_rows(raw_hist)
    model_hist = _render_hist(
        trim(win, limit=len(win)), name=char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": user_block}])
    try:
        raw = _llm_call(msgs, temperature=TEMPERATURE)
    except llm.PersonaChainExhausted:
        # ⚠️ 链路全挂不能吞成空回复：吞了就变成 502「她这轮没说话」，前端
        # 拿不到 relay_down、引导弹窗永远不弹（2026-11-09 真机实测）。
        # 让它冒到 persona_api 的 errorhandler，回 503 让前端弹窗。
        raise
    except Exception:
        log.exception("扮演轮调用失败 %s", key)
        return Turn("", "")
    # 服饰：演员这一轮交的就是新的当前穿着；没交作业沿用上一轮，不清零。
    outfit = split_outfit(raw) or prev_outfit
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    if not reply:
        # 整轮空 = 模型没交作业。这属于可用性问题，值得说一句，
        # 但**不重跑**：重跑一次未必更好，还白烧一轮。
        log.warning("扮演轮空回复 %s", key)
        return Turn("（我这会儿有点卡，你再说一句？）", "")

    # 女生主动表白（2026-11-08）：第一段正常回完之后，本轮结算的表达到了
    # 满值、感情线也到了「认准你了」，再让演员补一段把话说出口。
    # 历史里也要带上这一段，否则下一轮模型不知道她已经表白过。
    if v2 and isinstance(aff_res, dict) and aff_res.get("auto_confess"):
        extra = _confession_reply(head, model_hist, reply, card,
                                  user_block=user_block)
        if extra:
            body, meta = _split_meta(raw)
            raw = body.rstrip() + "\n\n" + extra + (meta or "")
            reply = reply.rstrip() + "\n\n" + extra
            log.info("v2 主动表白第二段 %s（%d 字）", key, len(extra))

    # 落历史：存**完整原文**（含「画面 / 心情 / 状态」三行）。2026-11-02 改：
    # 这三行是下一轮的 few-shot 示范，剥掉的话模型第 2 轮起就把格式丢了
    # （实测第 1 轮完美、第 2 轮全空）。展示时由 `split_turn` 再剥一次。
    #
    # 2026-11-02 补：漏格式的那几轮要先**补成合规形状**再存。否则历史里
    # 混着「没有三行」的回复，下一轮看到的就是「原来可以不写」，越写越散。
    #
    # 2026-11-05：用户消息额外挂 `seg`（分段）。原文 `content` 不动，
    # 网页回看/归档照旧；送模型时由 `_render_hist` 再拼成结构化块。
    new_user = {"role": "user",
                "content": _manual_display(man, text) if man else said}
    if segments:
        new_user["seg"] = segments
        new_user["addressed"] = addressed
    raw_hist.append(new_user)
    raw_hist.append({"role": "assistant", "content": _ensure_meta(
        raw, scene, mood, intensity, state, outfit)})
    # 2026-11-10：落盘就是**下一轮要送的窗口**，逐轮只追加、到高水位才砍回。
    # 以前每轮 `[-HISTORY_LIMIT:]`，聊过 6 轮后窗口每轮前移一条，前缀缓存
    # 全部错位（hit 永远只有 system 头那 15,104）。全量原文另有归档，不丢。
    try:
        _save_hist(key, cache_rows(raw_hist))
    except Exception:
        log.exception("扮演轮落史失败 %s", key)

    # 归档（2026-11-03）：窗口只留 12 条给模型看，**存档要全**。
    # 老会话第一次补写时拿当前窗口当起点——更早的已经没了，救不回来。
    # 归档只存 role/content（分段是模型内部格式，不给回看页看）。
    try:
        archive_rows = [{"role": m["role"], "content": m["content"]}
                        for m in raw_hist[-2:]]
        _append_archive(key, archive_rows,
                        seed=trim(raw_hist[-HISTORY_LIMIT:]))
    except Exception:
        log.exception("归档失败 %s", key)

    # 情绪与状态落盘（2026-11-02）：只覆盖不累加——情绪是当下的，
    # 状态是这一格的结果，下一轮读 `state` 续着演。
    _save_mood(key, mood, intensity, state, prev_state, outfit=outfit)

    _bump_turn(key)
    # 长期记忆（2026-11-05）：先攒进缓冲，每 N 轮才在后台提炼成事实入库。
    # 只写用户可见的话 + 她的回复；「想」不进去（见 `_visible_user_text`）。
    # 自定义模型配置要在这里取一份：后台线程没有请求线程的线程本地覆盖。
    if rag_on:
        try:
            persona_rag.remember(
                key, _visible_user_text(segments, said), reply,
                custom=llm.get_custom_override(),
                day=day_before,
                phase=str((gd or {}).get("phase") or ""),
                card_name=_card_label(card))
        except Exception:                                # noqa: BLE001
            log.exception("回忆入库失败 %s", key)
    return Turn(reply, scene, mood, intensity, state, aff_res, finish)


# 「睡到明天」时塞给她的话。2026-11-06 起这段**拼在 system 末尾**，
# 不再以 user 身份塞在历史最后——玩家语域里混进指令腔会把角色带跑。
#
# 2026-11-05 修（睡到第二天剧情严重不连贯）：接住前情写、挂着的约定继续、
# 昨晚没收尾的先交代收尾。
# 2026-11-06 再改（白象效应）：原文里三条「不要/别」把想避开的写法点亮一遍，
# 全部换成「可以这样写」的镜头清单；只给落点，不列禁区。


# 跳很多天 + 玩家写了「这段时间在干什么」（2026-11-06 用户提的）。
#
# 原来的多天跳转只有一句「写这段时间之后她这边的变化」——模型拿到的信号是
# 「跳过」，于是交出来的是压缩后的结论（「一周后，你们到了」）。可玩家心里
# 想的是**过程**：一路赶路、风餐露宿、路上说的话。跳 7 天不是想省 7 天，
# 是想把 7 天的路走完。
#
# 所以玩家给了内容就换这条：按他写的主线把过程写出来，结尾落在「现在」。

# 同一天切时段（2026-11-05）：不是「睡到明天」，所以不用「新的一天」那套，
# 重点是**把刚才那段收尾**、再进入新时段。玩家把早晨的早餐拖到中午，
# 她可以抱怨、追问——跳转本身就代表上一段 do 结束了。

_RECAP_MSGS = 2       # 只带跳跃前最后一幕（他一句 + 她一句）
_RECAP_CHARS = 700    # 单条上限；按**句**截断，不切半句

# 中文句末（含引号收尾）。用于「按句截断」——90 字硬切会把每条都切成
# 半句话，模型拿到 12 条语义残缺的碎片，只能另起一个头。
_SENT_END_RE = re.compile(r"[。！？!?…]+[」』”）)]*")


def _brief(text, limit=_RECAP_CHARS):
    """把一段正文整理成可读的样子：保留段落、**按句**截断。

    2026-11-06：原来是 `s[:90]` 硬切。实测每条精确 90 字，结尾是
    「…是想喝牛奶还是想喝豆浆，那我」「…昨天」这种半句。现在超长时截到
    limit 以内**最后一个句末标点**，宁可少给一段也不给断句。
    段落换行留着：这一幕是要给模型照着文风接着写的，压成一行就只剩情节了。
    """
    s = str(text or "").replace("\r", "")
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n")).strip()
    if len(s) <= limit:
        return s
    window = s[:limit]
    ends = [m.end() for m in _SENT_END_RE.finditer(window)]
    for end in reversed(ends):
        if end >= int(limit * 0.4):          # 别为了一个句号只留开头 20 字
            return window[:end].rstrip()
    return window.rstrip() + "…"


def _recap_block(session_key):
    """跨日/切时段的「前情」：只给跳跃前**最后一幕**的完整原话。

    2026-11-06 重写（用户：切换时间之后 AI 给的内容很差）：
    旧版把最近 6 轮按 90 字硬切成 12 条清单，每条都断在半句上；这 1199 字
    又和后面 12 条原始历史讲的是同一段事，一边是残缺摘要、一边是原文，
    互相打架。模型手里没有「昨晚停在哪」，只能另起一个头。

    现在只给一幕：完整、按句收尾、和历史里的原文一致。挂着的约定、所在地、
    昨晚的状态本来就在 `_gameday_block` / `build_head` 里，这里不再重复一遍。

    归档里「她先开口」的跳跃轮没有配对的 user（这是设计，不是断腿），
    所以按**消息序列**取尾部两条：孤儿轮只写她那一半，接着她那句往下写。
    """
    rows = [m for m in (load_archive(session_key) or [])
            if isinstance(m, dict) and m.get("role") in ("user", "assistant")]
    tail = rows[-_RECAP_MSGS:]
    lines = ["【前情 · 跳跃前停在这里】",
             "（下面是刚才最后一幕的原话。镜头就从这里抬起来，接着往下写。）"]
    for m in tail:
        if m.get("role") == "user":
            body = _brief(_visible_user_text(m.get("seg"), m.get("content")))
            who = "他"
        else:
            body, _sc, _mo, _it, _st = split_turn(str(m.get("content") or ""))
            body = _brief(body)
            who = "她"
        if body:
            lines.append("%s：%s" % (who, body))
    if len(lines) <= 2:
        return ""
    # 上一次跳跃是她先开的口：这一幕只有她的话，明说一句，读起来才不像漏了半轮。
    if not any(ln.startswith("他：") for ln in lines[2:]):
        lines.append("（上一轮是她先开的口，从她这句话接着写就行。）")
    return "\n".join(lines)


def _time_boundary(gd, phase=None):
    """跳跃轮最后一条 user：一句纯场景事实，标出「新的一轮从这里开始」。

    2026-11-06 D：任务指令全部搬进 system 末尾，玩家语域只留这一句时间
    标记。原来那条 user 写满了「【这一轮】…按平常的格式输出」，模型会把它
    当成玩家说的话，角色语域立刻乱掉。

    保留一条 user 有两个原因：OpenAI 兼容端点普遍以 user 作为一轮的开始；
    也让模型明确「昨晚那一幕已经收了，下面写新的一段」，而不是顺着上一条
    assistant 把昨晚继续写下去。
    """
    day = _day_of(gd)
    ph = str(phase or (gd or {}).get("phase") or "").strip()
    return "（第 %d 天%s）" % (day, ("，" + ph) if ph else "")



def jump_phase(session_key, phase, sender="", sender_uin=""):
    """切到今天的某个时段，并由她先开口收尾上一段、带入新时段（2026-11-05）。

    和 `skip_day` 的区别：**不推进天数**、不掷事件、不结算怀孕；但会
    `affection.sleep()` 把当前的亲密场景收掉（用户：跳转代表 do 结束），
    再把跳跃前最后一幕的完整原话拼进系统头，避免剧情断片。
    返回 `Turn`；没在演返回 None。
    """
    ph = normalize_phase(phase)
    if ph not in PHASES:
        return None
    key = resolve_line(_persona_key(session_key, sender_uin))
    card = active_card(key)
    if card is None:
        return None
    ent = _entry(key)
    if not ent:
        return None

    gd = _gameday(ent)
    old_phase = str(gd.get("phase") or "").strip() or "刚才"
    got = set_phase(key, ph)
    if not got:
        return None
    gd = _gameday(_entry(key) or ent)

    # 上一段 do 收场：场景结束、高潮值归零、冲动回落（好感/堕落不动）。
    try:
        affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        log.exception("时段跳跃收场失败 %s", key)

    ent = _entry(key) or ent
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")

    head = build_head(card, key, sender, note, prev_state, intimate=False)
    tblock = _gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    pblock = pregnancy_block(key)
    if pblock:
        head = head + "\n\n" + pblock
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    task = _PHASE_TASK % (old_phase, ph, _day_of(gd), ph)
    # 2026-11-06 D：指令归 system；最后那条 user 只留一句时间事实。
    head = head + "\n\n" + task

    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = cache_rows(_load_hist(key))
    model_hist = _render_hist(
        trim(win, limit=len(win)), name=char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 近因位跟普通轮同源：时间事实之后仍贴一句正向锚点，别让跳跃开场
    # 因为换了条组装路径就丢了视角。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": _time_boundary(gd, ph)
                + "\n\n" + writing_style.tail(
                    name=char_name(card, key=key))}])
    try:
        raw = _llm_call(msgs, temperature=TEMPERATURE)
    except llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        log.exception("时段跳跃开场失败 %s", key)
        return None
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    # 只归档她的开场，不归档那行系统任务。
    win.append({"role": "assistant",
                 "content": _ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        _save_hist(key, cache_rows(win))
        _append_archive(key, win[-1:], seed=trim(win[-HISTORY_LIMIT:]))
    except Exception:                                  # noqa: BLE001
        log.exception("时段跳跃落史失败 %s", key)
    _save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    _bump_turn(key)
    return Turn(reply, scene, mood, intensity, state, None, "")


def skip_day(session_key, sender="", sender_uin="", days=1, doing=""):
    """时间跳跃（2026-11-04；2026-11-05 支持多天）：推进 days 天，**由她先开口**。

    `doing`（2026-11-06）：玩家写的一句「这段时间在干什么」，比如
    「一路赶路去情爱城」。给了就按它写**过程**，而不是写成压缩后的结论——
    跳七天不是想省七天，是想把那七天的路走完。

    事件优先级在 `_roll_event`：约定 > 随机事件 > 普通的一天；跳多天时不掷
    日常小事件。跨天时先结算怀孕，再拼开场提示词。
    返回 `Turn`；没在演（没有会话条目）返回 None，调用方当普通消息处理。
    """
    try:
        days = max(1, min(365, int(days or 1)))
    except (TypeError, ValueError):
        days = 1
    doing = str(doing or "").strip()[:200]
    # 落一条日志（2026-11-06）：用户报「跳七天没按我写的过程走」，但从结果
    # 分不清是前端没把内容带上来、还是模型没照做。把入参记下来就能一眼判断。
    log.info("[skip] days=%s doing=%r", days, doing[:60])
    key = resolve_line(_persona_key(session_key, sender_uin))
    card = active_card(key)
    if card is None:
        return None
    ent = _entry(key)
    if not ent:
        return None

    old_day = _day_of(_gameday(ent))
    gd = _apply_gameday(key, force_new_day=True, days=days)
    new_day = int((gd or {}).get("day") or old_day)
    preg_event = None
    if pregnancy.card_enabled(card):
        try:
            preg_event = _preg_advance(key, old_day, new_day)
        except Exception:                              # noqa: BLE001
            log.exception("怀孕结算失败 %s", key)
    # `_preg_advance` 会重新读盘写盘，拿最新的条目，别用旧的 mood/note。
    ent = _entry(key) or ent
    note = str(ent.get("note") or "")
    prev_state = str(ent.get("state") or "")

    # 过夜收场（2026-11-05）：场景结束、高潮值归零、冲动回落。
    # 不这么做的话，第二天早上面板还挂着昨晚的高潮值，状态行也还是床上的
    # 衣着和位置，模型只能接着昨晚写——「睡了一觉什么都没变」。
    try:
        affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        log.exception("跨夜状态收场失败 %s", key)

    head = build_head(card, key, sender, note, prev_state, intimate=False)
    tblock = _gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    # 怀孕/孩子：跨天时先把结果告诉她，再让她开口。
    pblock = pregnancy_block(key, preg_event)
    if pblock:
        head = head + "\n\n" + pblock
    # 前情（2026-11-06）：只给跳跃前最后一幕的完整原话。挂着的约定/所在地
    # 已经在时间块里，这里不再重复一份半句清单。
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    event = str((gd or {}).get("event") or "").strip()
    if doing:
        # 玩家交代了过程：按他给的主线写这一段，不写「压缩摘要」。
        task = _SKIP_TASK_DOING % (days, new_day, doing, new_day)
    elif days == 1:
        task = _SKIP_TASK % (new_day,
                             ("（今天：%s）" % event) if event else "")
    else:
        task = _SKIP_TASK_MULTI % (days, new_day,
                                   ("（这段时间里：%s）" % event) if event else "")
    # 2026-11-06 D：这一轮该怎么写，全在 system 里说清；玩家语域只留一句
    # 时间事实，免得「按格式输出」这种系统腔被当成他说的话。
    head = head + "\n\n" + task
    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = cache_rows(_load_hist(key))
    model_hist = _render_hist(
        trim(win, limit=len(win)), name=char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 睡到明天同样贴近因位，和普通轮、时段跳跃共用一句。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": _time_boundary(gd)
                + "\n\n" + writing_style.tail(
                    name=char_name(card, key=key))}])
    try:
        raw = _llm_call(msgs, temperature=TEMPERATURE)
    except llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        log.exception("时间跳跃开场失败 %s", key)
        return None
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str(ent.get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    # 只归档她的开场，不归档那行系统任务（见 _SKIP_TASK 注释）。
    win.append({"role": "assistant",
                 "content": _ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        _save_hist(key, cache_rows(win))
        _append_archive(key, win[-1:], seed=trim(win[-HISTORY_LIMIT:]))
    except Exception:
        log.exception("时间跳跃落史失败 %s", key)
    _save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    _bump_turn(key)
    return Turn(reply, scene, mood, intensity, state, None)


# ── 高潮之后的三段请求（2026-11-05 晚，用户重新定的流程）──────
#
# 前 4 次：她到了 → 玩家在弹窗里选位置/自己写 → **立刻生成**射精那一下。
#          不用玩家再发一句话——他不用猜什么时候该写。
# 第 5 次：她到了 → 选位置 → 紧接着问「继续还是结束」：
#          · 结束   → 一次请求：射精 + 收场，留在当晚；
#          · 继续   → 请求 1：她累瘫了 + 最后一次（含射精）；
#                     页面中间浮出「第二天」，锁输入；
#                     请求 2：第二天早晨。
#
# 拆成三段是因为用户明确要「两次 AI 请求 + 中间一个跨天标识」——
# 一次请求写两段的话，跨天标识没地方插，读起来就是一段流水账。





# 自定义动作（玩家在弹窗文本框里自己写的）怎么交给编剧。



def finish_night(session_key, last=True, sender="", sender_uin=""):
    """第五次之后的两条出口（2026-11-05 用户拍板）。

    `last=True`  —— 再来最后一次，然后到第二天早晨（两段一个气泡）；
    `last=False` —— 就到这里，收场，留在当晚。

    玩家在弹窗里选完之后调这个：它**不再等玩家发消息**，直接生成。
    返回 `Turn`；没在演返回 None。
    """
def _gen_and_save(key, ent, card, head, task, sender="", prev_state="",
                  tag="收尾"):
    """公共尾巴：拿 head + task 调一次模型，剥格式、落史、落心情。

    `finish_action` / `finish_night` / `next_morning` 三处只差提示词，
    其余完全一样，抽出来免得三份各写一遍（写岔一次就有一处不落史）。
    """
    # 缓存滞回窗口（2026-11-10）：这三条旁路也要用整段窗口、并且照样只追加
    # 落盘，否则它们一跑就把主回复攒起来的前缀砍回 12 条，前功尽弃。
    win = cache_rows(_load_hist(key))
    model_hist = _render_hist(
        trim(win, limit=len(win)), name=char_name(card, key=key),
        alias=str((card or {}).get("name") or ""))
    # 收尾 / 第二天 / 第五次收场：这一轮的最后一条是任务指令，同样在它
    # 末尾贴统一锚点，让视角要求离生成点最近。
    msgs = ([{"role": "system", "content": head}] + model_hist
            + [{"role": "user", "content": task
                + "\n\n" + writing_style.tail(
                    name=char_name(card, key=key))}])
    try:
        raw = _llm_call(msgs, temperature=TEMPERATURE)
    except llm.PersonaChainExhausted:
        raise                                   # 见 run_turn：链路全挂要弹引导窗
    except Exception:                                  # noqa: BLE001
        log.exception("%s生成失败 %s", tag, key)
        return None
    reply, scene, mood, intensity, state = split_turn(
        raw, default_mood=str((ent or {}).get("mood") or ""))
    # 服饰（2026-11-09）：时段跳跃 / 隔天 / 收尾也会换衣服，状态一样要更新，
    # 否则「第二天换了身衣服」下一轮又被注回昨晚那件。没交作业就沿用。
    outfit = split_outfit(raw) or str((ent or {}).get("outfit") or "").strip()
    if not reply:
        return None
    win.append({"role": "assistant",
                 "content": _ensure_meta(raw, scene, mood, intensity, state, outfit)})
    try:
        _save_hist(key, cache_rows(win))
        _append_archive(key, win[-1:], seed=trim(win[-HISTORY_LIMIT:]))
    except Exception:
        log.exception("%s落史失败 %s", tag, key)
    _save_mood(key, mood, intensity, state, prev_state, outfit=outfit)
    _bump_turn(key)
    return Turn(reply, scene, mood, intensity, state, None)


def finish_action(session_key, spot="", text="", sender="", sender_uin=""):
    """她高潮之后，按玩家选的位置 / 自己写的做法，**立刻**写这一下。

    2026-11-05 晚改：以前是「选完等玩家再发一句话」，用户说那样很别扭——
    他得猜什么时候该写射精。现在选完直接生成一轮，玩家只负责推动剧情。

    `spot` 是预设位置（见 `FINISH_SPOTS`），`text` 是玩家自己写的做法；
    两个都给时**以 text 为准**（自定义比预设更具体）。
    返回 `Turn`；没在演返回 None。
    """
    key = resolve_line(_persona_key(session_key, sender_uin))
    card = active_card(key)
    if card is None:
        return None
    ent = _entry(key)
    if not ent:
        return None

    text = str(text or "").strip()[:120]
    spot = str(spot or "").strip().lower()
    if spot not in FINISH_SPOTS:
        spot = ""
    if not spot and not text:
        return None

    # 位置决定内射：怀孕只认这个，不靠编剧猜。
    finish = "none"
    if spot in ("pussy", "anus"):
        finish = "inside"
    elif spot in ("mouth", "body"):
        finish = "outside"
    day = _day_of(_gameday(ent))
    if finish == "inside":
        try:
            _preg_record(key, card, day, finish)
        except Exception:                              # noqa: BLE001
            log.exception("怀孕记录失败 %s", key)

    prev_state = str(ent.get("state") or "")
    head = build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=True)
    # 自定义优先，其次才是预设位置。
    hint = (_FINISH_CUSTOM_HINT % text) if text else finish_block(spot)
    if hint:
        head = head + "\n\n" + hint
    turn = _gen_and_save(key, ent, card, head, _FINISH_ACT_TASK,
                         sender=sender, prev_state=prev_state, tag="射精")
    # 写成功了才把待办清掉（2026-11-06）：失败时留着，页面刷新还能再问一次，
    # 不然就变成「数值扣了、文本没了」那种断层。
    if turn is not None:
        _set_pending_finish(key, None)
    return turn


def finish_night(session_key, last=True, sender="", sender_uin=""):
    """第五次之后的出口（2026-11-05）。

    `last=True`  —— 再来最后一次（只写这一夜，**不跨天**；天亮留给
                    `next_morning`，中间要插一个「第二天」标识）；
    `last=False` —— 就到这里，收场，留在当晚。

    玩家在弹窗里选完之后调这个，它不等玩家发消息，直接生成。
    """
    key = resolve_line(_persona_key(session_key, sender_uin))
    card = active_card(key)
    if card is None:
        return None
    ent = _entry(key)
    if not ent:
        return None

    # 玩家在弹窗里选的射精位置还没被用掉（第五次没有下一轮去消费它）。
    spot = take_finish(key)
    fblk = finish_block(spot)
    if last:
        # 只收场，不跨天——跨天由 `next_morning` 单独一次请求做。
        try:
            affection.sleep(key)
        except Exception:                              # noqa: BLE001
            log.exception("第五次收场失败 %s", key)

    ent = _entry(key) or ent
    prev_state = str(ent.get("state") or "")
    head = build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=True)
    if fblk:
        head = head + "\n\n" + fblk
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    task = _FINISH_LAST_TASK if last else _FINISH_END_TASK
    return _gen_and_save(key, ent, card, head, task, sender=sender,
                         prev_state=prev_state, tag="第五次收尾")


def next_morning(session_key, sender="", sender_uin=""):
    """第二天早晨（2026-11-05）：跨天 + 一次开场。

    单独一个函数是因为前端要在**两次请求之间**浮出「第二天」那个标识：
    先写完夜里，再显示跨天，再写早上。合成一次就插不进去了。
    """
    key = resolve_line(_persona_key(session_key, sender_uin))
    card = active_card(key)
    if card is None:
        return None
    ent = _entry(key)
    if not ent:
        return None

    old_day = _day_of(_gameday(ent))
    gd = _apply_gameday(key, force_new_day=True, days=1) or _gameday(ent)
    new_day = int((gd or {}).get("day") or old_day)
    preg_event = None
    if pregnancy.card_enabled(card):
        try:
            preg_event = _preg_advance(key, old_day, new_day)
        except Exception:                              # noqa: BLE001
            log.exception("怀孕结算失败 %s", key)
    # 跨天之后这一场必然结束了，再收一次场（幂等）。
    try:
        affection.sleep(key)
    except Exception:                                  # noqa: BLE001
        log.exception("跨天收场失败 %s", key)

    ent = _entry(key) or ent
    prev_state = str(ent.get("state") or "")
    head = build_head(card, key, sender, str(ent.get("note") or ""),
                      prev_state, intimate=False)
    tblock = _gameday_block(gd)
    if tblock:
        head = head + "\n\n" + tblock
    pblock = pregnancy_block(key, preg_event)
    if pblock:
        head = head + "\n\n" + pblock
    recap = _recap_block(key)
    if recap:
        head = head + "\n\n" + recap
    return _gen_and_save(key, ent, card, head, _FINISH_MORNING_TASK,
                         sender=sender, prev_state=prev_state, tag="第二天")


def _save_archive(session_key, rows):
    """整份重写归档（回退用）。只有 `rollback_last_turn` 会删归档条目，
    平时只增不删；写法和 `_save_hist` 一致，走原子替换防半截文件。"""
    p = _archive_path(session_key)
    if not p:
        return
    from app.core.agents import _atomic_write_text
    parent = os.path.dirname(p)
    if parent:
        os.makedirs(parent, exist_ok=True)
    _atomic_write_text(p,
                       "".join(json.dumps(m, ensure_ascii=False) + "\n"
                               for m in rows))


def rollback_last_turn(session_key, sender_uin=""):
    """回退上一轮（2026-11-04）：发错消息的后悔药。

    只支持**一级**——「上一条」的上一条退不了，不是时光机。一轮要退的账
    有五处，这里一次清完：

      1. 历史窗口 + 归档：删掉最后一轮（她的回复 + 你的那句话；
         「睡到明天」的开场只有她一条，就只删一条）；
      2. 好感：`affection.rollback_last` 按结算前快照整体还原；
      3. 游戏日：还原这轮推进前的 `gameday`（跨天/约定也随之退回）；
      4. 心情/状态：还原这轮覆盖前的那份（状态行是下一轮拼头用的）；
      5. 轮次计数 -1（小记压缩的节拍器）。

    返回 dict：`ok` / `error` / 还原后的 mood、intensity、state、day。
    没在演、没有可退的轮，`ok=False` 并带原因——调用方把原因带给用户。
    """
    key = resolve_line(_persona_key(session_key, sender_uin))
    ent = _entry(key)
    if not ent:
        return {"ok": False, "error": "这条线还没有在演的角色"}
    # 只允许一级（2026-11-06 用户要求）：退过一次就必须先出新的一轮，
    # 否则窗口尾部还是「她的一条回复」，会一轮一轮把整条历史拆掉。
    if ent.get("rollback_done"):
        return {"ok": False,
                "error": "刚回退过一次了，先和她重新说一句，才能再回退"}

    # ── 历史 + 归档 ─────────────────────────────────
    hist = _load_hist(key)
    if not hist or str((hist[-1] or {}).get("role")) != "assistant":
        return {"ok": False, "error": "没有可回退的轮（最新一条不是她的回复）"}
    n = 2 if (len(hist) >= 2
              and str((hist[-2] or {}).get("role")) == "user") else 1
    tail_pat = [str((m or {}).get("role")) for m in hist[-n:]]
    del hist[-n:]
    _save_hist(key, hist)

    # 归档只在**文件真实存在**时动：`load_archive` 对缺失文件会退回窗口副本，
    # 拿副本当账本删会把窗口里更早的轮也误删掉、还落成一个假归档文件。
    # 另外尾部形状要和窗口删掉的那轮一致——不一致说明归档和窗口已经错位
    # （铺底/写失败），宁可不删这条也不能错删。
    arch = _read_archive_file(key)
    if arch and [str((m or {}).get("role")) for m in arch[-n:]] == tail_pat:
        del arch[-n:]
        _save_archive(key, arch)
    else:
        log.warning("归档缺失或尾部和窗口对不上，回退只删了窗口 %s", key)

    # ── 游戏日 / 心情状态 / 轮次 ─────────────────────
    gd_prev = ent.pop("gameday_prev", None)
    if isinstance(gd_prev, dict):
        ent["gameday"] = gd_prev
    # 怀孕/孩子跟着这一轮一起退（2026-11-05）：快照里没有 preg = 回退前没怀。
    if "preg_prev" in ent:
        pp = ent.pop("preg_prev")
        if isinstance(pp, dict):
            ent["preg"] = pp
        else:
            ent.pop("preg", None)
    day = _gameday(ent).get("day")

    mp = ent.pop("mood_prev", None)
    if isinstance(mp, dict):
        ent["mood"] = str(mp.get("mood") or "")
        ent["intensity"] = mp.get("intensity") or 0
        ent["state"] = str(mp.get("state") or "")
        # 老快照没有这个键，别把新服饰误清成空。
        if "outfit" in mp:
            ent["outfit"] = str(mp.get("outfit") or "")
    mood = str(ent.get("mood") or "")
    intensity = ent.get("intensity") or 0
    state = str(ent.get("state") or "")

    state_map = _load_state()
    state_map[key] = ent
    st_turns = int(ent.get("turns") or 0)
    ent["turns"] = max(0, st_turns - 1)      # `_bump_turn` 的逆操作
    if ent.get("turn_total") is not None:
        ent["turn_total"] = max(0, int(ent.get("turn_total") or 0) - 1)
    ent["rollback_done"] = True              # 锁死二级回退，等下一轮解开
    # 放行这一把键的天数倒退（`_save_state` 的单调闸门），不然回退会被自己挡住。
    ent["_day_back_ok"] = True
    _save_state(state_map)

    # 回退也把这一轮从回忆缓冲/最近一批入库里撤掉（2026-11-05）。
    # 只有「你的话 + 她的回复」这种成对轮才进过 RAG；时段跳跃 / 高潮后那几段
    # 只有她一条（n=1），本来就没记，不能顺手去弹缓冲里更早的一轮。
    if n == 2:
        try:
            persona_rag.forget_last(key)
        except Exception:                                # noqa: BLE001
            log.exception("回退回忆失败 %s", key)

    # ── 好感（快照还原；自由模式没结算过 → None，不算失败）────
    aff = affection.rollback_last(key)

    return {"ok": True, "dropped": n, "aff": aff, "day": day,
            "mood": mood, "intensity": intensity, "state": state}


def _ensure_meta(raw, scene, mood, intensity, state, outfit=""):
    """把这一轮的回复规范成「正文 + 四行」的形状再进历史。

    模型有约三分之一概率漏写三行（契约再长也压不住）。直接把漏写的原文存进
    历史，等于给下一轮示范「可以不写」——越往后越散。所以这里补一个占位版本，
    缺什么补什么，让历史里每一条都是完整形状。

    补的值尽量从已有信息里取：画面缺就用空串（说明这轮没图），状态缺就沿用
    调用方给的值（`_save_mood` 已经在保留上一轮了）。

    `outfit`（2026-11-09）：当前穿着的权威串。原文里已经写了「服饰：」就
    原样留着；没写但调用方给了上一轮的值，就补一行——这样归档里每一轮
    都有服饰，点旧气泡出图时取的是**那一轮**的衣服，不是此刻的。
    不传就只保护原文里已有的行，老调用点行为不变。
    """
    body = str(raw or "").strip()
    if not body:
        return body

    def _has(rx):
        """标签存在**且后面真有内容**才算有；「画面：」后面空着不算。"""
        hits = list(rx.finditer(body))
        return any(h.group(1).strip() for h in hits)

    cur = split_outfit(body) or str(outfit or "").strip()
    # 服饰行齐了（或压根没有固化服饰）就不动原文
    if (_has(_SCENE_RE) and _has(_MOOD_RE) and _has(_STATE_RE)
            and (not cur or _has(_OUTFIT_RE))):
        return body
    plain, _, _, _, _ = split_turn(body)
    parts = [plain.rstrip()]
    if not _has(_SCENE_RE):
        parts.append("画面：" + (scene or ""))
    if cur and not _has(_OUTFIT_RE):
        parts.append("服饰：" + cur)
    if not _has(_MOOD_RE):
        parts.append("心情：%s 强度%d" % (mood or "平静", int(intensity or 0)))
    if not _has(_STATE_RE):
        parts.append("状态：" + (state or "未知"))
    return "\n".join(parts)


def _save_mood(session_key, mood, intensity, state, prev_state="", outfit=""):
    """把这一轮的情绪/状态/当前服饰写回会话条目。**已经出戏的不写。**

    `outfit`（2026-11-09）：演员这一轮交的「服饰：」行。空串 = 没交作业，
    沿用它上一轮已经写在 `st["outfit"]` 的值，不清零——和 mood/state 同一套
    「漏写不改状态」的规矩。

    2026-11-02：模型偶尔整轮不交三行（同一份契约、同一句话，实测有 1/6 概率
    全空）。这时候**保留上一轮的值**而不是写空——否则下一轮 `state` 变成空串，
    「她穿着什么」这条线就断了，出图又会回到裸体。
    """
    state_data = _load_state()
    st = state_data.get(session_key)
    if not isinstance(st, dict):
        return
    # 回退快照（2026-11-04）：这轮覆盖之前先留一份。回退上一轮时还原——
    # 状态行（她穿着什么/在哪）是下一轮拼系统头用的，留着一轮发错消息的
    # 状态，剧情会顺着错的方向演下去。
    st["mood_prev"] = {"mood": str(st.get("mood") or ""),
                       "intensity": st.get("intensity") or 0,
                       "state": str(st.get("state") or ""),
                       "outfit": str(st.get("outfit") or "")}
    if mood:
        st["mood"] = mood
    try:
        val = int(intensity or 0)
    except (TypeError, ValueError):
        val = 0
    if val or not st.get("intensity"):
        st["intensity"] = val
    if state:
        st["state"] = str(state)[:200]
        # 地点同步（2026-11-07）：状态行按契约是「地点 · 衣着 · 姿态」，
        # 第一段就是她此刻在哪，是模型每轮自己交的确定事实。以前 place
        # 只在打分器给了 score["place"] 时才更新，没给就一直空着，模型
        # 只能拿滞后账本里的旧地点猜——人在小超市，写的还是旧书市。
        spot = str(state).split("·")[0].strip()[:40]
        if spot:
            gd_now = _gameday(st)
            last = str(gd_now.get("place") or "").strip()
            # 同一地点连着写了几拍（2026-11-09）：只用来告诉模型「在原地转了
            # 几圈」，换不换场仍然它自己定。换地点归一，同地点累加；第一次
            # 有地点时从 1 起算。
            if last and last == spot:
                try:
                    gd_now["place_streak"] = int(gd_now.get("place_streak") or 1) + 1
                except (TypeError, ValueError):
                    gd_now["place_streak"] = 2
            elif last:
                gd_now["place_streak"] = 1
            gd_now["place"] = spot
        # 时段同步（2026-11-07）：状态行第四段是她**自己交的**「现在几点」。
        # 以前只有第一段（地点）被解析，时段永远是开场时的默认值——玩家看到
        # 深夜的正文、早晨的高亮，就是因为这一格没人写。
        # 兼容旧数据：老状态行只有三段，len < 4 时不动现有的 phase。
        seg = [x.strip() for x in str(state).split("·")]
        if len(seg) >= 4:
            ph = normalize_phase(seg[3])
            if ph in PHASES:
                gd_now = _gameday(st)
                gd_now["phase"] = ph
        # 接触位置（2026-11-07）：第五段，角色 AI 自己交的枚举。
        # 段数不足 5 = 这一轮没写，**保持上一拍不动**——空值不能当成「拔出来了」，
        # 那样一次漏写就会把状态机清零。
        if len(seg) >= 5:
            con = normalize_contact(seg[4])
            if con:
                gd_now = _gameday(st)
                gd_now["contact_prev"] = str(gd_now.get("contact") or "")
                gd_now["contact"] = con
    # 当前服饰（2026-11-09）：独立字段，别混进 `state` 那行中文里——
    # 出图要用的是这串英文定稿，也是下一轮回注给演员的事实。
    if str(outfit or "").strip():
        st["outfit"] = str(outfit).strip()[:300]
    _save_state(state_data)


# ─── 画面与出图 ──────────────────────────────────────

from app.features.persona.text_utils import (
    DRAW_INTENSITY, MOODS, _CLOTHES_ACTIONS, _CLOTHES_NOUNS, _MOOD_RE,
    _OUTFIT_RE, _POV_CAMERA, _SCENE_NOISE_RE, _SCENE_RE, _STATE_RE, _parse_mood,
    _clean_scene,
    _fixed_block, _fix_rating, _rating_of, _strip_monologue_marks,
    _strip_monologue_tag, _strip_refusal, _wanted_rating, build_prompt,
    clothes_changed, image_caption, should_draw, split_outfit, split_scene,
    split_turn, strip_thought_marker,
)


def _session_target(session_key):
    """会话线 → `(target, target_id)`（`group_123` → `("group", "123")`）。

    **网页线（2026-11-04 补）**：`web_<qq>` / `web_<qq>__<卡名>` → `(None, qq)`。

    为什么 target 是 `None`：`image_jobs` 早就把 None 当「网页侧」——
    照常排进**同一条图片队列**（同渠道、同优先级、同一块显卡，用户要的
    「网页也走 QQ 那条队列」就是这个意思），但 worker 不往 QQ 发任何东西
    （`_notice` / `_deliver` 在 target 为 None 时直接返回），图落
    `output/web/生图页/`（`comfy_out.session_base` 的既有约定）。

    `target_id` 仍然带 qq：`enqueue` 的会话键是 `("", qq)`，每个网页用户
    各算各的在途张数。都填 None 会让所有人共用 `("", "")` 这一个额度。

    不映射 `web_` 的后果：这里返回 `("", "")` → `draw()` 认为「这条线没有
    队列身份」直接返回空，网页「出图」永远失败。实测 `logs/agent.log`
    只有「扮演出图」那行，后面既没有「扮演配图完成」也没有「没画出来」，
    就是卡在这个静默分支。

    返回 `("", "")` 只表示这条线真的没有队列身份（不该出图）。
    """
    key = str(session_key or "")
    target, _, tid = key.partition("_")
    if target == "web":
        qq = tid.split("__", 1)[0]
        return (None, qq) if qq and qq != "local" else ("", "")
    if target not in ("group", "private") or not tid:
        return "", ""
    return target, tid


# 2026-11-02 撤掉：原来这里有一段给「不许裸」追加负面词（`nude, naked,
# topless...`）的 `_apply_nude_neg`，理由是 silver 那套 LoRA 裸体倾向强。
#
# 实测推翻了那个判断：几轮出裸图的提示词里**一个服装词都没有**——模型只写了
# 动作、表情、镜头，没写穿什么，模型只能落回先验。补上 `grey hoodie,
# lounge shorts` 之后立刻就穿回去了，负面词根本没参与。
#
# 而且 cfg=1 的 turbo 档里负面词本就没多少效果，那段反而会把「她真的脱了」
# 这个场景一并压住——用户要的恰恰是「状态里写什么就是什么」。所以整段删掉，
# 服装完全交给正面提示词里 `状态` 行翻译出来的标签。


# 出图失败原因（2026-11-08）：`draw()` 的返回值被十几个调用方和单测当字符串用，
# 不改签名；失败原因放在 thread-local 上，由同一个后台线程里的 `draw_shot()`
# 取走当 note。网页端 `/api/persona/shot_status` 会把 note 原样回给前端，玩家
# 才分得清「排队满 / 没在线 / 生成超时 / 跑完没出图」到底是哪一种，而不是
# 一律看到那句笼统的「排队满 / ComfyUI 没在线 / 被审核拦下」。
_DRAW_ERR = threading.local()


def _draw_fail(reason):
    """记下这一轮没画出来的真实原因，返回值仍是 ""（不改 draw 的签名）。"""
    _DRAW_ERR.reason = reason
    log.info("扮演配图没画出来：%s", reason)
    return ""


def draw(scene, card, session_key, reply_to="", state="", skill=None,
         landscape=False, on_submit=None, outfit=""):
    """出一张配图，返回文件名（没画出来返回 ""）。**阻塞**——调用方放后台线程。

    走 `SHOT_SKILL`（角色扮演默认 `silver`，两段采样，横屏竖屏同一条渠道，
    画幅只由 `landscape` 决定）。排队走**正常 QQ 队列**，不插队。
    入队带 `quiet=True`：worker 只跑图不发，图由调用方拿去 `_deliver`（要带
    caption 和引用，见 `qq_bot._draw_persona`）。

    `reply_to`（2026-11-02）：玩家触发这一轮的那条消息 id。**必须在消息线程上
    传进来**——`enqueue` 的 `reply_to=None` 会去读 `qq_api.current_message_id()`，
    而本函数跑在后台线程，那时上下文早不是本轮了，读出来是空或者别的轮次。
    这就是「图不引用消息」的根因。

    `outfit`（2026-11-09）：这一轮固化的当前穿着（英文），由 `shot_outfit()`
    从那一轮的归档取，前置到画面行之前。`state` 是中文状态行，只给
    `clothes_changed()` 判要不要出图，不参与拼提示词。

    也不扣私聊每日额度：扮演态配图是聊天的一部分，不是「点单生图」。队列满 /
    被审核拦下时，`_draw_persona` 那条自动配图一律静默跳过，聊天照常；
    `draw_shot()`（网页「出图」/ QQ 点单）会读 `_DRAW_ERR` 把真实
    原因回给玩家。两者共用同一个 `draw()`，返回值始终是字符串。
    ComfyUI 正在重启时**照常入队**（2026-11-09）——worker 提交前会等它回来，
    不再把重启窗口顶回成一次失败。
    """
    from app.features.image import jobs as image_jobs
    from app.tools.normal import generate_image as gi

    _DRAW_ERR.reason = ""       # 同一个线程可能被复用，先清掉上一轮的原因
    # 机位固定拼，主体由角色固定块声明一次（`build_prompt`）。
    # 2026-11-08 撤掉出图前的 tag 自动修：演员写的是英文散文长句，不是
    # Danbooru tag，那套漫画判据在这里 0 命中、纯空转；自动补 `1other`
    # 反而会把视角方实体化（漫画工作台 2026-10-12 的旧事故）。
    # 代码只检测事实，画面写什么交给模型。
    prompt = build_prompt(card, scene, state, pov=True, outfit=outfit)
    target, target_id = _session_target(session_key)
    # `target == ""` 才是「这条线没有队列身份」。不能用 `not target`：网页线
    # 的 target 是 None（见 `_session_target`），那也是合法身份。
    if not _clean_scene(scene) or not prompt or target == "":
        return _draw_fail("这一轮没有可画的画面")
    seed = random.randint(0, 2 ** 32 - 1)
    chan = skill or FAST_SKILL
    # 角色卡上写了 `char_lora` 就硬挂（2026-11-08 用户拍板）：卡已经知道
    # 她是谁，不用再让角色库猜一遍。没写就原样（旧行为，走向量召回）。
    wf = gi.build_t2i_workflow(
        chan, prompt, seed,
        user_text=str((card or {}).get("name") or ""),
        key=str((card or {}).get("char_lora") or ""))
    if wf is None:
        log.warning("扮演配图渠道 %s 没有文生图工作流", chan)
        return _draw_fail("渠道 %s 没有文生图工作流" % chan)
    # 探活**不再拒单**（2026-11-09 用户拍板，对齐 QQ 侧 2026-10-19 的口径）：
    # 从前这里探不到就当场 `_draw_fail`，等于把每一个 ComfyUI 重启窗口（实测
    # 14~22 秒）变成一次失败——而 worker 提交前本来就会 `wait_comfy_up` 等它
    # 回来（见 image_jobs._worker）。现在照常入队，`comfy_down` 只影响回执措辞。
    comfy_down = not image_jobs.comfy_alive()
    try:
        # landscape=False 写死：这里跑在后台线程，读不到本轮原话（qq_api 的
        # 上下文是 threading.local），不能让 enqueue 自己去判——判不出会退回
        # 「本轮原话里有没有横屏」，而这里恒为 None。
        job, reason = image_jobs.enqueue(
            target, target_id, wf, skill=chan, prompt=prompt,
            seed=seed, landscape=bool(landscape), quiet=True,
            # 输出目录（2026-11-07 用户点名）：扮演的配图**不许**再混进
            # `web/生图页/`——那是手动出图的地方，两边的图混在一起找不回来。
            # `job.out_dir` 优先于按 target 推断（漫画工作台走的是同一条路）。
            out_dir=PLAY_OUT_DIR,
            comfy_down=comfy_down,
            reply_to=str(reply_to or ""))
    except Exception as e:
        log.exception("扮演配图入队失败 %s", session_key)
        return _draw_fail("排队时出了岔子：%s" % e)
    if reason:
        log.info("扮演配图没排上（%s）：%s", session_key, reason)
        return _draw_fail("没排上：%s" % reason)
    # 提交成功那一刻给调用方一个钩子（2026-11-05 用户要求）：出图要几十秒到
    # 一两分钟，不回一句「已提交」，用户根本不知道这单到底排上没有。
    # 只传 job 不传调度细节——回执文案由调用方决定（QQ 用卡片发，网页蹲轮询）。
    if on_submit is not None:
        try:
            on_submit(job)
        except Exception:
            log.exception("扮演配图回执失败 %s", session_key)
    try:
        entry = job.wait()
    except Exception as e:
        log.info("扮演配图没画出来（%s）：%s", session_key, e)
        return _draw_fail("生成没成：%s" % e)
    names = image_jobs.output_images(entry, job=job)
    if not names:
        log.warning("扮演配图跑完却没有图片（%s）", session_key)
        return _draw_fail("ComfyUI 跑完了，但这一轮没拿到图片")
    log.info("扮演配图完成 %s：%s（%s）", session_key, names[0], prompt[:60])
    return names[0]


_NOTE_LOCK = threading.Lock()
_NOTE_RUNNING = set()
# 补压时最多回看多少条归档（2026-11-07）。压缩是补「上次存档之后」的
# 全部对话，拖得久时一次补齐；给上限是为了不把整条老线灌进一次 LLM。
NOTE_MAX_ROWS = 40


def _start_note_compress(session_key):
    """起一次后台压缩；同一条线已经有线程在跑就不再起。

    `note_due` 压缩失败不清零，下一轮会重试。没有这把锁时，模型连续 503
    就会每轮叠一个线程，全部读同一份旧账本再抢着写回。
    """
    with _NOTE_LOCK:
        if session_key in _NOTE_RUNNING:
            return False
        _NOTE_RUNNING.add(session_key)

    def _run():
        try:
            compress_note(session_key)
        finally:
            with _NOTE_LOCK:
                _NOTE_RUNNING.discard(session_key)

    threading.Thread(target=_run, name="persona-note", daemon=True).start()
    return True


def _bump_turn(session_key):
    """轮次 +1；攒够 NOTE_EVERY_TURNS 轮就在后台压一次小记。

    放后台：压缩要一次 LLM 调用，不该让第 10 轮那个人的回话凭空慢几秒。

    **节拍另记一份单调值（2026-11-07）**：`turns` 会被回退减 1，而玩家
    「聊一条退好几次」时净增长长期为 0，`NOTE_EVERY_TURNS` 永远够不着，
    账本就停在前一天不动（实测 11-07 全天 94 次聊天 / 28 次回退，
    账本一次都没更新）。`note_due` 只增不减，回退不碰它——被撤掉的
    那轮不进历史，但「生成过一轮」是既成事实，该压的还是要压。
    """
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return
    st["turns"] = int(st.get("turns") or 0) + 1
    if "note_due" not in st:
        # 老线接上已有的节拍，不从零重新攒（否则已经滞后的还得再等 5 轮）。
        st["note_due"] = max(0, int(st.get("turns") or 0) - 1)
    st["note_due"] = int(st.get("note_due") or 0) + 1
    # 累计轮次（2026-11-06 用户要看的「聊了多少轮」）：`turns` 是每 5 轮
    # 清零的压缩节拍，这里另记一份永不归零的总量。老线第一次补算时归档
    # 已经包含这一轮，所以直接取她的回复条数，不再 +1。
    if st.get("turn_total") is None:
        # 归档正常时它已经包含这一轮；归档缺失（老线/写失败）至少算这一轮。
        st["turn_total"] = max(1, _archive_assistant_count(session_key))
    else:
        st["turn_total"] = int(st.get("turn_total") or 0) + 1
    # 新的一轮已经落史：刚才那级回退用完了，允许再退一次。
    st.pop("rollback_done", None)
    _save_state(state)
    if st["note_due"] >= NOTE_EVERY_TURNS:
        _start_note_compress(session_key)


# ── 开场导读（2026-11-04）──────────────────────────────
#
# 用户的原话：「开始聊天的时候给我网页一段灰色的摘要，先说明当前是什么情况，
# 不然我就莫名其妙地开始了，都不知道怎么聊。至少要有一个背景吧，这样 AI 和
# 我才能知道怎么去开始。」
#
# 所以它不是她的台词，是给玩家看的**场景说明**：时间、地点、她正在干什么、
# 你们是什么关系、从哪儿开口。只在没有历史的新线上生成一次，缓存进 state。
OPENING_MAX = 260


_DAYPARTS = ((5, "深夜"), (8, "早上"), (11, "上午"), (13, "中午"),
             (17, "下午"), (19, "傍晚"), (23, "夜里"), (24, "深夜"))


def _daypart():
    """现在大概是一天里的什么时候（只给个粗粒度，写进导读就够用）。"""
    h = datetime.datetime.now().hour
    for edge, name in _DAYPARTS:
        if h < edge:
            return name
    return "深夜"


def opening(session_key, force=False):
    """这条线的开场导读；生成过一次就缓存。

    没有角色时返回空串（网页不画那块灰条）。生成失败也返回空串——开场导读
    是个锦上添花的东西，不能因为它挂掉整页打不开。
    """
    card = active_card(session_key)
    if card is None:
        return ""
    ent = _entry(session_key) or {}
    cur = str(ent.get("intro") or "").strip()
    if cur and not force:
        return cur
    # 卡级默认开场（2026-11-07）：卡里写了就直接用，**不再调模型**。
    # 优先级：session 的 `intro` > 卡的 `opening` > 现生成。
    # 为什么 session 优先：玩家已经聊过的线不该被卡里的开场覆盖——
    # 那等于把开场白换成另一段，历史还在，两边的场景对不上。
    card_open = str(card.get("opening") or "").strip()
    if card_open and not force:
        return card_open

    rel = ""
    try:
        st = affection.status(session_key) or {}          # 顺手建好感记录
        bits = [str(st.get("levelName") or ""), str(st.get("tierName") or "")]
        rel = "、".join([b for b in bits if b]) or "还不熟"
    except Exception:                                     # noqa: BLE001
        rel = "还不熟"

    who = "，".join([b for b in (
        char_name(card, "", key=session_key),
        str(card.get("description") or ""),
        str(card.get("personality") or "")) if b])
    sysp = str(card.get("systemPrompt") or "").strip()
    if sysp:
        who += "\n" + sysp[:260]
    prompt = _OPENING_PROMPT % (OPENING_MAX, who, rel, _daypart())
    try:
        text = str(_llm_call([{"role": "user", "content": prompt}],
                             max_tokens=700) or "").strip()
    except Exception:                                     # noqa: BLE001
        log.exception("开场导读生成失败 %s", session_key)
        return ""
    if not text:
        return ""
    text = _cut_sentence(text, OPENING_MAX)
    state = _load_state()
    st = state.get(session_key)
    if isinstance(st, dict):
        st["intro"] = text
        _save_state(state)
    log.info("开场导读已生成 %s（%d 字）", session_key, len(text))
    return text


def _note_convo(session_key, st):
    """取「上次压缩之后」的全部对话，返回 `(消息, 归档总行数)`。

    以前读的是窗口 12 条——和压缩周期（5 轮 ≈ 10 条）几乎是同一批内容。
    一旦因为回退或模型失败拖了几轮才压上，中间那几轮既不在窗口里、
    也没进过任何一版账本，剧情的发展线路就永久断了（2026-11-07 用户报
    「AI 根本不知道前面几个轮次聊的事情的发展线路」）。改读归档里
    `note_at` 之后的部分，滞后多少补多少；老线没归档或增量为空时退回窗口。
    """
    arch = _read_archive_file(session_key)
    arch_len = len(arch)
    rows = []
    if arch:
        at = max(0, int(st.get("note_at") or 0))
        rows = (arch[-NOTE_MAX_ROWS:] if at <= 0
                else arch[min(at, arch_len):][-NOTE_MAX_ROWS:])
    hist = _render_hist(rows) if rows else []
    if not hist:
        hist = _render_hist(trim(_load_hist(session_key)))
    return hist, arch_len


def _settled_day_facts(gd):
    """给账本压缩器的「系统结算事实」清单：只报事实，不下结论。

    2026-11-10 用户报「昨天约好的事已经做完了，第二天她还当没做」。
    根子在压缩器只看得见旧账本 + 最近对话，看不到这里结算过的天数、
    已办完的事和约定的日期，于是把做过的约定一代代抄回待办节。
    """
    gd = gd if isinstance(gd, dict) else {}
    day = _as_int(gd.get("day"), 0, 0, 99999)
    facts = ["游戏内第 %d 天 · %s"
             % (day, str(gd.get("phase") or "时段未定"))]
    if str(gd.get("place") or "").strip():
        facts.append("所在地：" + str(gd["place"]).strip())
    ev = str(gd.get("event") or "").strip()
    if ev.startswith("已经办完"):
        facts.append("今天已经办完："
                     + ev[len("已经办完"):].lstrip("：:").strip())
    elif ev.startswith("约定："):
        # 23213a7 之前生成的老存档：那句「约定：「X」」本来就是兑现记录。
        facts.append("今天已经办完：" + ev[len("约定："):].strip("「」"))
    pl = str(gd.get("plan") or "").strip()
    if pl:
        pd = _as_int(gd.get("plan_day"), 0, 0, 99999)
        facts.append("系统登记过的约定：%s%s"
                     % (pl, ("（约在第 %d 天）" % pd) if pd else ""))
        if pd and pd <= day:
            facts.append("约的那天已经过了；做没做过以最近对话为准。")
    else:
        facts.append("系统里目前没有登记在册的约定。")
    facts.append("约定是否已经兑现，以【最近发生的】对话为准，系统登记只作参考。")
    return "\n".join("- " + f for f in facts)


def compress_note(session_key):
    """把旧账本 + 上次存档以来的对话合并成一份 ≤1000 字的 Markdown 账本。

    2026-11-05 改：从「一段散文」改成「六个固定小节」。原来每次压缩都是拿
    「上一版摘要 + 最近 12 条」重写，聊到一百轮等于把最早的事摘要了二十遍，
    每遍丢一点。现在给旧账本一个明确的形状，并要求「还成立的条目原样抄」，
    只让模型改变化的部分——代际退化会小很多。
    """
    card = active_card(session_key)
    if card is None:
        return False
    st = _entry(session_key) or {}
    # 旧账本先纠人称再交给压缩器（2026-11-06）：不让「他」以「原样抄」的形式
    # 传给下一版，否则坏账本会永久自复制。
    old = _ledger_fix_pronouns(st.get("note") or "").strip()
    hist, arch_len = _note_convo(session_key, st)
    if not hist:
        return False
    convo = "\n".join(
        "%s：%s" % ("女主角" if m["role"] == "assistant" else "玩家", m["content"])
        for m in hist)
    # 跨天事实（2026-11-10）：压缩器以前只看得见「旧账本 + 最近对话」，
    # 看不到系统结算的「今天已经办完 / 约定约在第几天」，又要求旧条目原样抄，
    # 于是把已经做完的约定一代代抄回待办节。这里只把**事实**列给它，
    # 做没做完由它对照对话自己判断，代码不替它下结论。
    gd = st.get("gameday") if isinstance(st.get("gameday"), dict) else {}
    prompt = _NOTE_PROMPT % {
        "soft": NOTE_SOFT,
        "name": char_name(card, key=session_key),
        "day_facts": _settled_day_facts(gd),
        "old": old or "（还没有）",
        "convo": convo,
    }
    try:
        # 输出上限要显式给：1000 字中文约 1500 token，不给的话服务端
        # 默认上限可能截在半句话上（同 `moss_comic.write` 的坑）。
        note = str(_llm_call([{"role": "user", "content": prompt}],
                             max_tokens=2400) or "").strip()
    except Exception:
        log.exception("小记压缩失败 %s", session_key)
        return False
    if not note:
        return False
    # 模型没按账本格式交（少小节、整段散文、前后灌了废话）→ 保留旧账本。
    # 宁可记旧事，不能拿一份残的覆盖掉——这是「记忆越压越少」的根。
    if not _ledger_ok(note):
        log.info("小记没按账本格式交，保留旧的 %s：%r", session_key,
                 note[:80])
        return False
    state = _load_state()
    st = state.get(session_key)
    if not isinstance(st, dict):
        return False                          # 压缩期间出戏了，别把它写回去
    # 压缩期间玩家按了「回退」：归档尾部已经被删掉，这份摘要里含着被撤掉
    # 的那轮。写回去等于把没发生的事记成真事，放弃，下一轮重压。
    if len(_read_archive_file(session_key)) < arch_len:
        log.info("压缩期间发生回退，本次账本不落盘 %s", session_key)
        return False
    st["note"] = _clip_ledger(note, NOTE_MAX_CHARS)
    st["turns"] = 0
    st["note_due"] = 0
    st["note_at"] = arch_len
    _save_state(state)
    log.info("账本已更新 %s（%d 字，原始 %d，补压 %d 条）", session_key,
             len(st["note"]), len(note), len(hist))
    return True
