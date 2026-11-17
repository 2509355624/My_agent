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
# 2026-11-17 用户拍板：写作 AI **固定**最近 6 轮，不再用 30 条滞回或 9000 字
# 滑窗（真实账单证明历史没进缓存，长窗口只是纯增加 miss）。落盘/归档仍保留
# 长窗口，回看一条不少。
#
# 记性由三层兜底：12 条固定窗口 + 500~1000 字小记（每 5 轮压一次）+ 状态里的
# 「她此刻的样子」。小记 5 轮 < 窗口 6 轮，刚好接得上，不会出现「挤出窗口、
# 还没进小记」的空档；少了的远景细节由小记和 RAG 补。
HISTORY_LIMIT = 12

# 磁盘窗口上限（2026-11-10 引入，2026-11-17 降级为纯落盘缓冲）。
#
# 真实账单里写作调用命中一直卡在 14,115 token（正好 system 头），历史
# 7,100 token 每轮按 miss 重付；历史并没有跟着头一起进缓存。30 条长窗口
# 只把 miss 越堆越高，没有换来缓存收益。现在写作 AI 固定只送最近 6 轮
# （12 条，见 turn.py），这里的 30 条不再参与模型请求，只让网页回看/小记
# 有更长的原文缓冲；全量归档仍在 `_append_archive`，一条不少。
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
from app.features.persona.history_view import (
    _archive_assistant_count, _split_meta, archive_len, cache_rows, history_page,
    load_archive, trim, turn_total,
)
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
from app.features.persona.finish import (
    _climax_info, _clear_adult_night, _gen_and_save, _recap_block, _set_pending_finish,
    finish_action, finish_block, finish_night, next_morning, pending_finish, set_finish,
    take_finish,
)
_DRAW_ERR = threading.local()


from app.features.persona.persona_shots import (
    _shot_parts, draw_shot, is_shot_request, shot_outfit,
)
from app.features.persona.persona_images import (
    _draw_fail, _session_target, draw,
)
from app.features.persona.lines import (
    _group_redirect, _match_card_in_text, resolve_line, status_brief, switch, switch_line,
)
from app.features.persona.opening import opening
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






def shot_scene(session_key, msg_index=None):
    """「出图」这一轮的「画面：」行（英文定稿）。没有就返回空串。

    没有画面行就**直接提示没得画**（2026-11-04 用户拍板），不做 LLM 兜底
    翻译——正常轮次的输出契约保证每轮都有画面行，空是极少数边缘情况。
    """
    return _shot_parts(session_key, msg_index)[2]






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












