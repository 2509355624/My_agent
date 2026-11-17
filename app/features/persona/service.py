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
from app.features.persona import mood as _mood_store
from app.features.persona import gameday
from app.features.persona.wipe import clear, erase as _erase, wipe
from app.features.persona.commands import handle_command
from app.features.persona.gameday import _apply_gameday
_DRAW_ERR = threading.local()


from app.features.persona.persona_images import (
    _draw_fail, _session_target, draw,
)
from app.features.persona.notes import (
    NOTE_MAX_ROWS, _bump_turn, _note_convo, _settled_day_facts, compress_note,
)
from app.features.persona.rollback import _save_archive, rollback_last_turn
from app.features.persona.turn import run_turn
from app.features.persona.gameday import (
    game_day, game_phase, jump_phase, set_phase, skip_day,
)
from app.features.persona.judge_fields import (
    NOTE_MAX_CHARS, _director_fields, _item_keys_text, _judge_ledger,
    _norm_finish,
)
from app.features.persona import judge
from app.features.persona.judge import (
    INTENTS, judge_v2 as _judge_v2, score_profile as _score_profile,
)
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
from app.features.persona.cards_lookup import (
    USER_CARD_MAX, USER_FIELD_MAX, _INT_FIELD_MAX, _clean_user_fields,
    _name_taken_error, _write_user_cards, edit_user_card, find_card,
    get_card, get_user_card, load_user_cards, make_user_card,
    remove_user_card, user_cards_path,
)

from app.features.persona.ledger_text import (
    _LEDGER_SECTIONS, _PRONOUN_HE, _clip_ledger, _clip_note, _cut_sentence,
    _ledger_fix_pronouns, _ledger_ok,
)




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
# ── 本轮事实点（2026-11-08）──────────────────────────────
# 玩家一段话里并列的道具、动作，最容易在第一段被「做→一句话概括」压成
# 「他拿出食材」，演员再写就只剩最后一句台词（拿出了鸡蛋，却被反问
# 「要不要加个鸡蛋」）。读原文的是编剧，所以让它顺手列一张清单；代码只
# 裁剪和透传，落地交给演员——和 segments / items 同一个分工。
#
# 清单还兼一个作用：契约里有「你话里交代的前因，她没跟着去过」，场景义务
# 又要求「他做的动作要演出来」。玩家写「我把鸡蛋拿出来」时模型会摇摆，
# 于是把摆好的东西当没发生。这里正面说清「就在这一幕眼前」，不动契约正文。


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


def _judge_intent(card, said, state="", history=None, note=""):
    """判断这句话在干什么；LLM 与名字解析由门面注入。"""
    return judge.judge_intent(
        card, said, state=state, history=history, note=note,
        llm_call=_llm_call, name_fn=char_name, recent_fn=_judge_recent)


def _judge_intent_scored(card, said, state="", line_key="", gameday=None,
                         history=None, note=""):
    """攻略模式第一段：意图 + 关系打分；LLM 与画像由门面注入。"""
    return judge.judge_intent_scored(
        card, said, state=state, line_key=line_key, gameday=gameday,
        history=history, note=note, llm_call=_llm_call,
        profile_fn=_score_profile, recent_fn=_judge_recent)


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
    """把这一轮的情绪/状态/服饰写回；漏写的字段保留上一轮。"""
    return _mood_store.save(session_key, mood, intensity, state, prev_state, outfit,
                     load_state=_load_state, save_state=_save_state)


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


# 补压时最多回看多少条归档（2026-11-07）。压缩是补「上次存档之后」的
# 全部对话，拖得久时一次补齐；给上限是为了不把整条老线灌进一次 LLM。






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






