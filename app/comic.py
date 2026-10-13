"""漫画工作流调度器：剧本 → silver 出场景图 → qwen 加气泡台词。

两个现成的生图工作流（`skills/silver` 文生图、`skills/qwen_image_v1` 图生图）
本身一个字节都不改，这里只补它们之间缺的那层调度：

- **自己给自己编号**：每格用剧本里的格号（01、02…）命名文件（`raw/01_raw.png`
  → `final/01_final.png`）。ComfyUI 的自动编号（Anima_00087 那种）不连续、还会
  随重启跳号，所以**从不拿它当匹配依据**——阶段二认的是我们自己写的文件名。
- **两阶段批量跑**：先把所有格用 silver 渲染完，再统一换 qwen 加气泡。按模型
  分批而不是逐格交替，避免显存来回切换。
- **逐张落盘 + state.json**：每跑完一张就把状态写回磁盘，ComfyUI 中途重启、
  或某张失败，重跑时自动跳过已完成的格，接着往下跑。

用法：
    python -m app.comic new "<一句话设定>" --panels 3   # 编剧层写剧本 + 直接开跑
    python -m app.comic write "<设定>" --out comics/Cxxx/script.md  # 只写剧本
    python -m app.comic run comics/C20261007-01/script.md
    python -m app.comic run <script> --only 01,02      # 只跑指定格
    python -m app.comic run <script> --silver-only     # 只跑阶段一（场景图）
    python -m app.comic run <script> --scene-skill nai # 阶段一改走 NAI 云端（快）
    python -m app.comic status <script>                # 看进度

渲染参数覆盖（LoRA 槽 / 两段采样 / 分辨率）按任务存在任务目录的 render.json，
网页端的「⚙ 渲染参数」卡维护它；run / render_scene 开跑时自动读，CLI 同样享受。

「写剧本」那一层在 app/comic_writer.py（LLM 写 + 按契约校验 + 不合格回炉），
它产出的是本模块认的 script.md，两层只靠这份契约耦合——换角色 / 换题材
只动编剧层，这里一个字不用改。
"""

import argparse
import json
import logging
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime

import requests

from app import comfy_src, image_jobs, comic_render_opts
from app.config import BASE_DIR, COMFYUI_OUTPUT_DIR, COMFYUI_URL
from app.skills import load_skill, load_workflow
from app.tools.normal import generate_image

log = logging.getLogger("comic")

# 阶段一 / 阶段二的渠道（见 skills/ 下各自的 SKILL.md）
# 2026-10-11 用户拍板：漫画场景渲染从 silver 换成 anima-line（anima 线条，
# 6 LoRA 堆叠、两段链 2456×3686）——线条感更强，适合漫画格。它是逐格单张
# 调 build_t2i_workflow，只要工作流有提示词占位符就通（已确认）。
SCENE_SKILL = "anima-line"        # 文生图：出没有字的场景图
BUBBLE_SKILL = "qwen_image_v1"    # 图生图：在场景图上加对话框

# 阶段一也可以走 NAI 云端（`--scene-skill nai`）：它是 `image_jobs.NAI_SKILLS`
# 里的渠道，走云端通道、不碰 ComfyUI。调它只为「快」——本地 silver 每张要
# ~30 秒且会触发 ComfyUI 换渠道重启，NAI 云端两张并发、单张几秒。验证气泡
# 效果时用它做场景图，能把等待时间压掉大半。
# 注意 NAI 是**标签式英文**模型（同 anima_*），`画面:` 字段照旧写英文标签即可。

# qwen 的图生图骨架里 denoise 写死 1、没有 `__DENOISE__` 占位符，这个数传进去
# 是空操作——留着只是对齐 generate_image 的调用约定。
I2I_DENOISE = "0.60"

# 阶段二的提示词模板。qwen 这边的**中文约束是有效的**：node 20 是
# TextEncodeQwenImage21，挂的文本编码器是 Qwen3-VL 8B（中文原生），正向提示词
# 直接写中文即可，不用翻英文。
#
# ⚠️ 负向提示词在当前配置下**不起作用**：KSampler 的 cfg=1.0，CFG 的公式是
# 「负向 + cfg×(正向−负向)」，cfg=1 时负向项被完全抵消（12 步蒸馏模型的推荐采样
# 参数就是 cfg=1）。所以约束只能写在正向里——**而写在正向里的否定句反而诱发
# 幻觉**：否定一件事，就等于把那个东西的名字喂给模型（白熊效应，2026-10-11
# 用户点名）。实测后果：模板里「不要指向女生」让尾巴更容易挂到女生身上。
#
# 模板按 Qwen 官方的「文本渲染 / 编辑指令」写法组织（2026-10-11 第 9 轮重整）。
# 官方原话来自 Qwen-Image-Edit 官方 Space 内置的 Edit Instruction Rewriter：
#   - `All text content must be enclosed in English double quotes " ".`
#     —— **引号就是唯一标记**，没有 EXACT 之类的前缀。之前写「文字（EXACT）："…"」，
#     成品气泡里真画出了「EXACT」这个单词。
#   - 「加新文字」与「替换文字」同属文本替换任务，写法 `Replace "xx" to "yy"`。
#   - 位置 / 颜色 / 排版只在需要时才写；总则要求**简洁、避免长句与多余描述**。
#   - 否定内容走 `negative_prompt` 参数（阿里云 API 有独立通道，官方示例传空格），
#     正向只写要什么：「不要复杂背景」→「纯色背景」。
# 社区实测同向：`Keep everything else unchanged` 要显式加；「明确说保留什么」比
# 「说删除什么」稳得多（只写删除有 12% 误删邻近元素，补上保留项后 0%）。
#
# 于是本文件三条铁律：
#   ① 正文里不出现任何否定句，条数与归属一律用**正面清点**（「画面里的文字一共
#      这几处：…」）；
#   ② 精确文本只用英文双引号包住，不加任何标记词；
#   ③ 不写序号、【】、括号解释——那是给人看的，模型会当素材；
#   ④ 形状与位置本身就是归属的语义，**不再用文字解释「这是谁说的」**
#      （2026-10-11 用户拍板，见 `_bubble_sec`）。
#
# ⚠️ 气泡那一段（{sec}）**不在模板里写死**，由 `_bubble_sec` 按**说话人**现拼：
#   角色的台词 → 圆形气泡 + 写明指向谁；
#   「我们」（镜头视角）的台词 → 白色圆角矩形框 + 固定右下角 + **一个字都不提
#   指向**（2026-10-11 用户拿两张实测图拍板：形状和位置本身就是归属的语义，
#   再补一句"这是我们说的话"不会帮到模型，只会多一个幻觉入口）。
BUBBLE_TEMPLATE = (
    "{sec}"
    "{count}"
    "气泡放在人物脸部以外，人物、服装、姿势、背景和画风保持不变。"
)

# 第二个气泡模板（第二遍 i2i 用）。和第一个的区别有两个，都是实测踩出来的：
#   ① 位置在**右上角**——第一个气泡在左上，两个不重叠；
#   ② 数量清点必须把**已有的那个气泡算进去**——此时画面里已经有第一个气泡了，
#      只报「一共这一处」等于让模型把已有的当成多余的那个抹掉或重画。
# 位置参数化（{pos}）：这一格若还带旁白条（BUBBLE_TEMPLATE_LABEL 也画在右上角），
# 第二个气泡必须让位到右下角，否则两个文字元素叠在同一角落（2026-10-11）。
BUBBLE_TEMPLATE_2 = (
    "画面中已经有一个对话框，保持它的大小、位置、内容和样式不变。"
    "{sec}"
    "画面里的文字一共这两处，就是原有的那个对话框和这个新对话框。"
    "气泡放在人物脸部以外，人物、服装、姿势、背景和画风保持不变。"
)

# 双文字模板：气泡之外**再加一行剧情旁白条**（画在画面右上角）。
# 拆成独立模板而不是给上面那个加可选段，是为了让「只写这一段」这条数量约束在
# 两种情况下都保持明确——实测约束写得越死，模型越不会自己加戏。
#
# ⚠️ 原文写的是「加一行**小**字」，实测 qwen 会照字面画成很小的字
# （2026-10-11 用户反馈「作为标签的字体有点小」）。字号必须显式写清楚，
# 「小字」这种词只能删掉——它对模型就是个字号指令。
#
# 旁白可以有多条（剧本里 `标签1:` `标签2:`…，见 parse_script_text），这一段由
# `_label_sec` 现拼进 {lab_sec}：单条和多条的措辞不同，多条的必须逐行点名。
BUBBLE_TEMPLATE_LABEL = (
    "{sec}"
    "{lab_sec}"
    "气泡和旁白都放在人物脸部以外，人物、服装、姿势、背景和画风保持不变。"
)

# 一次画完两个气泡（2026-10-11）。
#
# 此前是「第一遍画气泡1+旁白，第二遍拿第一遍的成图当源图补气泡2」——每个带第二句
# 台词的格多花一次 50~70 秒。当初拆两遍是因为一次画两个会崩（初版 5 格里 2 格冒出
# 第二个乱气泡），但那版模板只说了「加一个对话框」：**条数没声明、归属没声明**，
# 模型只能自由发挥。后来每条气泡都必须声明「文字 + 尾巴指向 + 位置」，诱因没了——
# 真跑 3/3 全对，热机 69 秒（与单气泡那遍一模一样，多画一个气泡不额外耗时）。
#
# 位置沿用两遍版最终的分布（旁白占右上角，气泡2 因此让到右下角），所以观感不变，
# 只是少跑一遍。界面仍留「分开画」开关（render_bubble 的 split）：某格一遍画崩了
# 可以用两遍单独重跑。
BUBBLE_TEMPLATE_ALL = (
    "在这张漫画格里添加两个对话框（气泡）。"
    "{sec1}"
    "{sec2}"
    "两处的文字各自只写自己那一段。"
    "{lab_sec}"
    "气泡和旁白都放在人物脸部以外，人物、服装、姿势、背景和画风保持不变。"
)


# 镜头视角的「我们」。它的台词固定右下角、白色圆角矩形框，**不写任何指向**
# —— 2026-10-11 用户拍板：形状 + 位置就是约定，再用文字解释归属只会多一个
# 幻觉入口（他手写的实测版里，我方那一段连"箭头""尾巴"两个字都没出现）。
_SELF_SPEAKER = "我"
_SELF_POS = "右下角"


def _bubble_sec(pos, speaker, text, intro=True, again=False):
    """一个气泡 / 对话框在提示词里的那一段。

    官方写法：位置 + 形状 + 引号包裹的精确文本 + 排版。**不带序号、不带标记词**
    —— `【气泡1】` 和 `文字（EXACT）` 都是给人看的，模型会把它们当素材
    （EXACT 真被画进过成品气泡里）。

    两种形态（取决于它是单独出现，还是被拼进「一次画两个」的合并版）：
      · `intro=True` —— 自带引导句（「在这张漫画格里添加一个对话框（气泡），
        位置在画面…的空白处」）。单气泡、第二遍 i2i 走这条；
      · `intro=False` —— 直接「{pos}的对话框里写 …」，合并版把它按序拼在
        自己的开头句后面（不然会出现"添加两个对话框"接"添加一个对话框"）。

    按**说话人**走两条完全不同的写法：
      · **角色的台词**——白色圆角框、黑色细边框，落在 pos，写明指向谁；
      · **「我们」（镜头视角）的台词**——白色圆角矩形框，固定右下角，
        **箭头 / 尾巴这两个字一个都不出现**（两种形态下写法一致）。

    `again=True` 是「第二遍 i2i 在成图上补第二个气泡」的措辞（split 退路）。
    """
    text = _clean_dialogue(text)
    if speaker == _SELF_SPEAKER:
        return ('仅在画面%s添加一个白色的圆角矩形框。'
                '框里写 "%s"，文字排成 2 到 3 行居中显示，每行不超过 8 个字，'
                '字迹清晰。' % (_SELF_POS, text))
    tail = _tail_note(speaker)
    if not intro:
        return ('%s的对话框里写 "%s"，它是白色圆角框、黑色细边框，%s，字体为黑体。'
                % (pos, text, tail))
    if again:
        return ('在画面%s再添加一个对话框：白色圆角框、黑色细边框，%s，字体为黑体。'
                '这个新对话框里写 "%s"，文字排成 2 到 3 行居中显示，'
                '每行不超过 8 个字，字迹清晰。' % (pos, tail, text))
    return ('在这张漫画格里添加一个对话框（气泡），位置在画面%s的空白处。'
            '气泡是白色圆角框、黑色细边框，%s，字体为黑体。'
            '气泡里写 "%s"，文字排成 2 到 3 行居中显示，每行不超过 8 个字，字迹清晰。'
            % (pos, tail, text))


# 台词里的中日式引号（「」『』）会被 qwen 当成句子边界——实测 04 那格就是在「」处
# 把一句话断成了两个气泡。喂给模型前剥掉；精确文本由模板统一用英文双引号包住，
# 这样既满足「文本要加引号」，又不会触发中文引号的断句语义。
_QUOTE_CHARS = "「」『』“”‘’\"'"


def _clean_dialogue(text):
    out = "".join(ch for ch in (text or "") if ch not in _QUOTE_CHARS)
    return re.sub(r"\s+", " ", out).strip()


# ─── 气泡归属 / 落位 / 旁白段 ─────────────────────────
#
# 都是 2026-10-11 用户实测反馈后定型的：
#
# ① **归属必须说死**，但只用**最短的正向说法**。模板原来只说「带一个指向人物的
#    尾巴」，qwen 就一律把气泡判给画面里的女生——镜头外「我」说的话也变成她说的。
#    所以剧本里每条气泡都要声明说话人（`气泡: （我）…`）。
#    ⚠️ 两条禁区：
#      · 否定句（「不是女生说的」「不要指向女生」）——等于把那个词又喂一遍，
#        白熊效应；
#      · **多余的解释**（「说话人是画面外的我」）——2026-10-11 用户原话：
#        「我们参杂了大量的无效内容给 qwen，他不是思考模型，他是画图的模型，
#        所以没有用的词，比如禁止什么、不要什么，都不要说，只说我们要什么即可」。
# ② **「我们」不靠文字表身份。** 白色圆角矩形框 + 右下角就是约定，我方那一段
#    连「指向」都不写（见 `_bubble_sec` 的第一个分支）。
# ③ **多条旁白要逐行列出来。** 只说「加一行旁白」，qwen 只会画一行；`标签1:`
#    `标签2:` 这样的多段旁白必须一条一条告诉它第几行写什么。

def _tail_note(speaker):
    """角色气泡该指向谁——必须说死，含糊就挂错人。

    只给**最短的那条**正向线索：`指向画面中的女生`。
    2026-10-11 用户实测后砍掉了两处冗余：
      · 「尾巴」这个词本身（更早还写成「向下指向画面下缘」）——模型不需要知道
        气泡的尾巴朝哪，只需要知道**这气泡是谁的**；
      · 「我们」的台词**压根没有指向可写** → 返回空串，由 `_bubble_sec` 走
        另一条分支（白色圆角矩形框 + 右下角）。
    """
    return "" if speaker == _SELF_SPEAKER else "指向画面中的女生"


# 角色气泡的落位顺序：先左上，被占依次让到右上 / 右下 / 左下。
_ROLE_POS_ORDER = ("左上方", "右上角", "右下角", "左下角")


def _layout(speakers, has_labels):
    """给这一格的气泡分配落位，返回与 speakers 等长的位置列表。

    规矩（2026-10-11 用户拍板）：
      · 「我们」的台词**固定右下角**——位置 + 白色圆角矩形框就是它的身份；
      · 角色的台词按 `_ROLE_POS_ORDER` 挑第一个空位；
      · 带旁白时右上角让给旁白，不参与分配。

    两边都落到右下角的情况（例如连续两句都是「我」）不做特殊处理：位置重了
    也不会挂错人，用户原话「qwen 自己会理解的」。
    """
    taken = {"右上角"} if has_labels else set()
    out = []
    for sp in speakers:
        if sp == _SELF_SPEAKER:
            pos = _SELF_POS
        else:
            pos = next(p for p in _ROLE_POS_ORDER if p not in taken)
        taken.add(pos)
        out.append(pos)
    return out


def _count_note(bubbles, pos2, n_labels, pos1="左上方"):
    """「画面里一共这几处文字」——用**正面清点**说数量与归属。

    这条约束必须存在（初版完全不说，5 格里 2 格冒出乱气泡），但说法要从
    「不要出现第三个气泡」改成「一共这几处：…」——前者把「第三个气泡」这个词
    本身喂给了模型（白熊效应）。

    单独抽出来是因为它随三种情况变：画一个还是两个气泡、带不带旁白、旁白一行
    还是多行。原来这句写死在 `_label_sec` 里、永远说「左上角一个气泡」——
    合并版（一次画两个气泡，见 BUBBLE_TEMPLATE_ALL）复用它，就会在同一段
    提示词里一边说「一个气泡」一边说「两个气泡」，模型只能二选一。
    （2026-10-11 自查发现。与「`stepping off` 改用 `standing on one leg` 之类」
    同类：提示词里的字面描述对模型就是硬指令。）
    """
    names = ["%s的那个气泡" % pos1]
    if bubbles == 2:
        names.append("%s的那个气泡" % pos2)
    if n_labels == 1:
        names.append("右上角的一行旁白")
    elif n_labels:
        names.append("右上角的 %d 行旁白" % n_labels)
    total = {1: "一处", 2: "两处", 3: "三处"}[len(names)]
    return "画面里的文字一共这%s：%s。" % (total, "、".join(names))


def _label_sec(lines, bubbles=1, pos2="右下角", pos1="左上方"):
    """旁白段的提示词。单条 / 多条两种写法，多条的每行都要单独点名。

    bubbles / pos1 / pos2 只有**一次画两个气泡**时才会传非默认值：这一段收尾是
    一句数量清点，必须知道这一格一共画几个气泡、各自落在哪（见 `_count_note`
    和 `_layout`）。
    """
    n = len(lines)
    out = ["另外在画面右上角添加旁白文字：黑体，字号清晰可读、与气泡内文字大小相当，"
           "每条旁白最多排 3 行、每行不超过 8 个字（放不下就换到下一行），"
           "样式是纯文字加白色描边。"]
    if n == 1:
        out.append('这行旁白写 "%s"。' % _clean_dialogue(lines[0]))
    else:
        out.append("旁白一共 %d 行，在右上角从上到下依次排开，每行单独占一行、"
                   "行与行之间留出空隙：" % n)
        out.append("".join('第 %d 行写 "%s"；' % (i + 1, _clean_dialogue(s))
                           for i, s in enumerate(lines)))
    out.append(_count_note(bubbles, pos2, n, pos1))
    return "".join(out)

# 同 image_out / comfy_src：本机常驻代理会把 127.0.0.1 也劫持成 502，
# 取图这条回环请求必须 trust_env=False。
_session = requests.Session()
_session.trust_env = False


# ─── 剧本解析 ─────────────────────────────────────────
#
# 格式（Markdown，人写得动、机器好读）：
#
#     # 标题：夜色便利店
#     任务号: C20261007-01
#
#     ## 01
#     画面: 1girl, solo, silver hair, ...
#     气泡: 这么晚了，便利店还开着呢。
#     标签: 我攥住了她的书包带        # 剧情旁白条，画在右上角
#
#     ## 02
#     ...
#
# 字段名中英都认；画面/气泡各自可以续行（缩进或直接换行都行，遇到空行结束）。

_PANEL_RE = re.compile(r"^\s*##\s+(\d{1,3})\s*$")
# ⚠️ 交替是从左到右吃的，**带序号的写法必须排在裸名前面**：
#   「气泡2」排在「气泡」前，否则 `气泡2: …` 被吃成 field=气泡、值剩「2: …」；
#   「说话人2」排在「说话人」前，同一个坑；
#   「标签\d*」把多条旁白（标签1、标签2…）一并收进来。
_FIELD_RE = re.compile(
    r"^\s*(画面|场景|scene|prompt"
    r"|气泡2|台词2|bubble2|气泡|台词|bubble|dialogue"
    r"|说话人2|speaker2|说话人|speaker"
    r"|标签\d*|状态\d*|label\d*|stat\d*)"
    r"\s*[:：]\s*(.*)$", re.I)
_FIELD_MAP = {"画面": "scene", "场景": "scene", "scene": "scene", "prompt": "scene",
              "气泡": "bubble", "台词": "bubble", "bubble": "bubble",
              "dialogue": "bubble",
              "气泡2": "bubble2", "台词2": "bubble2", "bubble2": "bubble2",
              "标签": "label", "状态": "label", "label": "label", "stat": "label",
              "说话人": "speaker", "speaker": "speaker",
              "说话人2": "speaker2", "speaker2": "speaker2"}
# 多条旁白：`标签2` / `label3` 这类带序号的都归到 label（裸名在 _FIELD_MAP 里）
_LABEL_NUM_RE = re.compile(r"^(?:标签|状态|label|stat)\d+$", re.I)

# 气泡的**说话人**。必须在剧本里写死：qwen 只认「这个气泡是谁说的」，模板里说不清
# 它一律挂到画面里的女生头上——镜头外「我」说的话就变成她说的了
# （2026-10-11 用户实测：本该是视角方发言的气泡，被判给了女生）。
# 写法是气泡行里带前缀 `（女）` / `（我）`，渲染前由 `_split_speaker` 剥掉——
# 它只决定**这一段文字怎么写**（角色的圆气泡在左上、我们的圆角矩形框在右下，
# 见 `_bubble_sec` / `_layout`），**不进画面文字**。
_DEFAULT_SPEAKER = "女"
_SPEAKER_ALIAS = (("女生", "女"), ("妹妹", "女"), ("她", "女"), ("女", "女"),
                  ("哥哥", "我"), ("哥", "我"), ("他", "我"), ("男", "我"),
                  ("我", "我"))
_SPEAKER_BRACKET_RE = re.compile(
    r"^\s*[（(【\[]\s*([^）)】\]]{1,4})\s*[）)】\]]\s*[:：]?\s*")
_SPEAKER_COLON_RE = re.compile(r"^\s*([女男我她他哥妹][生哥妹]?)\s*[:：]\s*")
_TITLE_RE = re.compile(r"^\s*#\s*(?:标题\s*[:：]\s*)?(.+?)\s*$")
_TASKID_RE = re.compile(r"^\s*任务号\s*[:：]\s*(.+?)\s*$")
_TASKID_FORMAT = re.compile(r"^C\d{8}-\d+$")


def _field_of(raw):
    """字段名 → 内部键。`标签1` / `标签2` / `label3` 这类带序号的旁白都归到
    `label`（多条旁白就是这么写的）；认不出来返回 None。"""
    key = (raw or "").strip().lower()
    got = _FIELD_MAP.get(key)
    if got:
        return got
    return "label" if _LABEL_NUM_RE.match(key) else None


def _speaker_of(raw):
    """把模型写的说话人说法归一成「女」/「我」；认不出返回空串。

    归一成两个值就够：画面里出现的只有女生，说话人要么是她，要么是镜头外那个
    「我」（画面里只有他的手）。这两者的写法完全不同，见 `_tail_note`。
    """
    v = (raw or "").strip().strip("（）()【】[]：: ")
    for k, out in _SPEAKER_ALIAS:
        if k in v:
            return out
    return ""


def _split_speaker(text):
    """剥出气泡行里的说话人前缀，返回 (说话人, 干净台词)。

    认两种写法：`（女）台词` 和 `女：台词`。说话人**不进画面文字**，必须在渲染
    前剥干净；认不出就返回空串，由调用方回退默认值（画面里的女生）。
    """
    t = (text or "").strip()
    for rex in (_SPEAKER_BRACKET_RE, _SPEAKER_COLON_RE):
        m = rex.match(t)
        if not m:
            continue
        sp = _speaker_of(m.group(1))
        if sp:
            return sp, t[m.end():].strip()
    return "", t


def parse_script(path):
    """读剧本文件，返回 (meta, panels)。"""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return parse_script_text(text)


def parse_script_text(text):
    """解析剧本正文，返回 (meta, panels)。

    panels 是 dict 列表，含 no / scene / bubble / bubble2 / label / speaker /
    speaker2。解析逻辑只此一份——编剧层（app/comic_writer.py）写完剧本也调它做
    校验，所以拆出文本版。

    - **多条旁白**：`标签1:` `标签2:` `标签3:`（`标签:` 同义）按出现顺序各占一行，
      用 `\\n` 拼在 `label` 上。用户 2026-10-11 拍板：一格要能讲好几段剧情。
    - **说话人**：气泡行带前缀 `（女）` / `（我）` 时剥进 speaker / speaker2；
      单独写一行 `说话人:` 也认（补到最靠近的那个气泡上）。
    """
    meta = {}
    panels = []
    cur = None
    field = None
    pending_speaker = ""             # `说话人:` 写在气泡**前面**时先存着
    for raw in text.splitlines():
        line = raw.rstrip()

        m = _PANEL_RE.match(line)
        if m:
            cur = {"no": "%02d" % int(m.group(1)), "scene": "",
                   "bubble": "", "bubble2": "", "label": "",
                   "speaker": "", "speaker2": ""}
            panels.append(cur)
            field = None
            pending_speaker = ""
            continue

        if cur is None:                      # 面板之前的头部只认标题 / 任务号
            m = _TASKID_RE.match(line)
            if m:
                meta["task_id"] = m.group(1).strip()
                continue
            m = _TITLE_RE.match(line)
            if m:
                meta["title"] = m.group(1).strip()
                continue
            continue

        m = _FIELD_RE.match(line)
        if m:
            key = _field_of(m.group(1))
            val = m.group(2).strip()
            if key == "label":
                # 多条旁白：按出现顺序各占一行（`标签:` 与 `标签1:` 同义）
                cur["label"] = (cur["label"] + "\n" + val) if cur["label"] else val
            elif key == "speaker":
                sp = _speaker_of(val)
                if sp:
                    # 独立成行也认：写在气泡后面补给最近那个气泡，写在前面留给下一个
                    if cur["bubble2"]:
                        cur["speaker2"] = sp
                    elif cur["bubble"]:
                        cur["speaker"] = sp
                    else:
                        pending_speaker = sp
            elif key in ("bubble", "bubble2"):
                sp, body = _split_speaker(val)
                if not sp and key == "bubble" and pending_speaker:
                    sp = pending_speaker
                cur[key] = body
                cur["speaker2" if key == "bubble2" else "speaker"] = sp
                if key == "bubble":
                    pending_speaker = ""
            else:
                cur[key] = val
            field = key
            continue

        if not line.strip():
            field = None
            continue

        if field:                            # 续行：接到上一个字段上
            cur[field] = (cur[field] + " " + line.strip()).strip()

    return meta, panels


def update_panel_scene(script_path, no, new_scene):
    """把第 no 格的 `画面:` 行整体替换成 new_scene，**其他格一字不动**。

    用户 2026-10-12 的需求：补正/编剧把某个词改坏时（实测：close-up 格被删掉
    `navy pleated skirt`，裙子从画面上消失），他只想手改一个词就重画这一格，
    而不是推倒整份剧本重跑。整份 save 走 /api/comic/save，这里只服务「单格
    快改」。

    - no 用 "3" / "03" 都行，内部归一成两位。
    - new_scene 压成单行（画面行本来就是逗号标签清单，换行没有意义）。
    - 该格画面行后面如果挂着续行（旧解析器允许折行），一并丢弃——整行替换。
    - 返回 True=改了；False=没找到这一格（或格子里没有画面行）。

    改完不负责重跑：load_state 的契约（scene 变了 → 该格自动退回 pending、
    旧图作废）会让下一次 run(only={no}) 只重画这一格。
    """
    no = str(no or "").strip()
    no = no.zfill(2) if no.isdigit() else no
    scene = " ".join(str(new_scene or "").split())
    with open(script_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    out, cur, replaced, dropping = [], None, False, False
    for raw in lines:
        line = raw.rstrip()
        m = _PANEL_RE.match(line)
        if m:
            cur, dropping = "%02d" % int(m.group(1)), False
            out.append(line)
            continue
        if cur == no and not replaced:
            m = _FIELD_RE.match(line)
            if m and _field_of(m.group(1)) == "scene":
                out.append("画面: %s" % scene)
                replaced, dropping = True, True   # 旧续行作废，直到下一个字段/格
                continue
        if dropping:
            # 续行＝非空、不是字段、不是格标题。遇到三者之一就停止丢弃。
            if line.strip() and not _FIELD_RE.match(line):
                continue
            dropping = False
        out.append(line)

    if not replaced:
        return False
    with open(script_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return True


# ─── 任务目录 / 状态 ──────────────────────────────────

_STATE_LABEL = {"pending": "待渲染", "rendered": "待加气泡", "done": "已完成"}


class Panel:
    def __init__(self, no, scene, bubble, label="", bubble2="",
                 speaker="", speaker2=""):
        self.no = no
        self.scene = scene
        self.bubble = bubble
        self.bubble2 = bubble2      # 可空；有值时阶段二对成图再串一次 i2i 画第二个气泡
        self.label = label          # 可空；多条旁白用 \n 分隔（`标签1:` `标签2:`…）
        self.speaker = speaker or _DEFAULT_SPEAKER    # 气泡尾巴指向谁（见 _tail_note）
        self.speaker2 = speaker2 or _DEFAULT_SPEAKER
        self.state = "pending"      # pending → rendered → done
        self.seed = None
        self.error = None

    def labels(self):
        """旁白行——多条旁白（`标签1:` `标签2:`…）在这里拆开。"""
        return [x.strip() for x in (self.label or "").split("\n") if x.strip()]

    def two_bubbles(self):
        """这一格要不要画第二个气泡（`气泡2:` 非空）。"""
        return bool(self.bubble2.strip())

    def bubble_prompt(self, split=False):
        """阶段二的提示词。

        气泡的**落位由「谁说的」决定**（见 `_layout`）：我方固定右下角，角色的
        台词占左上为首的空角。有第二个气泡时**默认一次画完两个**
        （BUBBLE_TEMPLATE_ALL，2026-10-11 合并）；`split=True` 退回「第一遍只画
        气泡1+旁白」的老写法，配合 bubble2_prompt() 分两遍跑——界面上的
        「分开画」开关走这条路。
        """
        lines = self.labels()
        if self.two_bubbles() and not split:
            pos1, pos2 = _layout([self.speaker, self.speaker2], bool(lines))
            lab_sec = (_label_sec(lines, bubbles=2, pos2=pos2, pos1=pos1) if lines
                       else _count_note(2, pos2, 0, pos1))
            return BUBBLE_TEMPLATE_ALL.format(
                sec1=_bubble_sec(pos1, self.speaker, self.bubble, intro=False),
                sec2=_bubble_sec(pos2, self.speaker2, self.bubble2, intro=False),
                lab_sec=lab_sec)
        pos, = _layout([self.speaker], bool(lines))
        sec = _bubble_sec(pos, self.speaker, self.bubble)
        if lines:
            return BUBBLE_TEMPLATE_LABEL.format(
                sec=sec, lab_sec=_label_sec(lines, bubbles=1, pos1=pos))
        return BUBBLE_TEMPLATE.format(
            sec=sec, count=_count_note(1, None, 0, pos))

    def bubble2_prompt(self, pos="右上角"):
        """第二遍 i2i 的提示词。

        pos 由调用方给：这一格若带旁白条（它也画在右上角），第二个气泡必须
        让位到右下角，否则两个文字元素叠在同一个角落。落位同样受说话人约束
        ——「我」的台词一律右下角（`_bubble_sec` 里再兜一次）。
        """
        return BUBBLE_TEMPLATE_2.format(
            sec=_bubble_sec(pos, self.speaker2, self.bubble2, again=True))


def _guess_task_id(dirpath):
    name = os.path.basename(os.path.normpath(dirpath))
    if _TASKID_FORMAT.match(name):
        return name
    now = datetime.now()
    return "C%s-%s" % (now.strftime("%Y%m%d"), now.strftime("%H%M%S"))


class Task:
    """一个漫画任务 = 一个目录（script.md + raw/ + final/ + state.json）。"""

    def __init__(self, script_path, meta, panels):
        self.script_path = os.path.abspath(script_path)
        self.dir = os.path.dirname(self.script_path)
        self.meta = meta
        self.panels = [p if isinstance(p, Panel)
                       else Panel(p["no"], p.get("scene", ""),
                                  p.get("bubble", ""), p.get("label", ""),
                                  p.get("bubble2", ""), p.get("speaker", ""),
                                  p.get("speaker2", ""))
                       for p in panels]
        self.task_id = meta.get("task_id") or _guess_task_id(self.dir)
        self.raw_dir = os.path.join(self.dir, "raw")
        self.final_dir = os.path.join(self.dir, "final")
        self.state_path = os.path.join(self.dir, "state.json")

    def raw_path(self, p):
        return os.path.join(self.raw_dir, "%s_raw.png" % p.no)

    def final_path(self, p):
        return os.path.join(self.final_dir, "%s_final.png" % p.no)

    def load_state(self):
        """把 state.json 里记着的进度贴回面板，实现断点续跑。"""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("state.json 读不出来（%s），当作全新任务", e)
            return
        saved = data.get("panels") or {}
        for p in self.panels:
            s = saved.get(p.no)
            if not isinstance(s, dict):
                continue
            p.state = s.get("state", p.state)
            p.seed = s.get("seed", p.seed)
            # 失败原因也必须贴回来（2026-10-12）。不贴的后果是一条完整的死路：
            # `/status` 每次都从磁盘重读（`_load_task` → `load_state`），error
            # 恒为 None → 前端既不显示「出错」badge、也不给重跑按钮；而失败的
            # 格恰好**停在 pending**（失败时没改 state），于是界面显示的是
            # 「排队中…」，用户以为它在排队，其实早就死了、还无从重跑。
            # 实测 comics/C20261012-03 格01：state.json 里明明记着
            # ConnectionResetError(10054)，load 完却是 0 格有 error。
            p.error = s.get("error", p.error)
            # 剧本改了就把旧图作废：场景变了 → 整格重来；只改了气泡/标签 →
            # 场景图还能用，退到「待加气泡」重跑阶段二。两种情况都要清掉旧
            # error——它描述的是上一版剧本的失败，留着会一直在界面上挂着「出错」。
            if s.get("scene") and s["scene"] != p.scene:
                p.state, p.seed, p.error = "pending", None, None
            elif (p.state == "done"
                  and (s.get("bubble", "") != p.bubble
                       or s.get("bubble2", "") != p.bubble2
                       or s.get("label", "") != p.label
                       or s.get("speaker", "") != p.speaker
                       or s.get("speaker2", "") != p.speaker2)):
                p.state, p.error = "rendered", None

    def save_state(self):
        os.makedirs(self.raw_dir, exist_ok=True)
        os.makedirs(self.final_dir, exist_ok=True)
        data = {
            "task_id": self.task_id,
            "title": self.meta.get("title", ""),
            "script": os.path.basename(self.script_path),
            "updated": datetime.now().isoformat(timespec="seconds"),
            "panels": {
                p.no: {
                    "scene": p.scene,
                    "bubble": p.bubble,
                    "bubble2": p.bubble2,
                    "label": p.label,
                    "speaker": p.speaker,
                    "speaker2": p.speaker2,
                    "state": p.state,
                    "seed": p.seed,
                    "error": p.error,
                    "raw": self._rel(self.raw_path(p)),
                    "final": self._rel(self.final_path(p)),
                }
                for p in self.panels
            },
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _rel(self, path):
        return os.path.relpath(path, self.dir).replace("\\", "/")


# ─── 生图（走 image_jobs 队列，target=None = 网页侧，不往 QQ 发）──

def _new_seed():
    return random.randint(0, 2 ** 31 - 1)


def _require_comfy():
    if not image_jobs.comfy_alive():
        raise SystemExit("ComfyUI 没在线（%s 连不上），先把它开起来再跑。"
                         % COMFYUI_URL)


def _submit(workflow, skill, prompt, seed):
    """排进全局串行队列并等出图，返回 ComfyUI 那边的输出文件名。

    target=None 是「网页侧」路径：worker 出图后只把 history entry 挂到 job 上，
    不发回任何会话、也不走审核（见 image_jobs.process）。所以这里拿到 entry
    自己把图取下来落盘即可。
    """
    job, reason = image_jobs.enqueue(None, None, workflow, skill=skill,
                                     prompt=prompt, seed=seed, landscape=False)
    if reason:
        raise RuntimeError(reason)
    entry = job.wait()                       # 失败会抛 job.error
    names = image_jobs.output_images(entry)
    if not names:
        raise RuntimeError("跑完了但没找到输出图片")
    return names[0]


# 重试**只针对超时**这一种失败。实测它的成因有两类，都不是提示词的问题：
#   ① 冷启动——刚换渠道 / ComfyUI 刚重启，模型还在加载，第一张顶满 180 秒；
#   ② 内存紧张——加载把可用内存压到 COMFY_MIN_FREE_RAM_GB(3.0) 以下，触发
#      RAM 守卫重启 ComfyUI，正在跑的那张被打断（实测内存 4.4GB→2.1GB 那次）。
# 两类都是「等它喘过来就好」：重试时模型已在显存里、或守卫刚清完场，基本一次
# 就过（实测首张 180 秒超时 → 重试 46 秒成功）。其余错误（提示词非法、连不上
# ComfyUI、找不到输出）重试没意义，直接抛。
_TIMEOUT_HINT = "超时"

# 下载重试窗口（秒）与间隔。**只重取文件，不重跑生成**——见 `_download`。
_DOWNLOAD_RETRY_SECS = 180
_DOWNLOAD_RETRY_GAP = 3


def _submit_retry(make, what, tries=3):
    for attempt in range(1, tries + 1):
        try:
            return make()
        except Exception as e:
            if _TIMEOUT_HINT not in str(e) or attempt == tries:
                raise
            log.warning("%s 超时（第 %d 次，共 %d 次），重试",
                        what, attempt, tries)


def _download(filename, dst):
    """把 ComfyUI 那边的输出图取到本地。

    **失败不再一次判死，重试到窗口用完**——这个错几乎总是「图已经画好、
    正在取文件时 ComfyUI 被重启」：RAM 守卫（`_maybe_restart_for_ram`，水位
    `COMFY_MIN_FREE_RAM_GB`=3.0）在跑完一张后把 ComfyUI 重启掉，而同一时刻
    客户端正在 `/view` 取这张图。现场见 logs/agent.log：
        00:36:34,105 系统可用内存只剩 2.6GB（低于 3.0GB 水位），重启 ComfyUI 释放
        00:36:34,870 [格 01] 场景渲染失败：ConnectionResetError(10054 ...)
    两次丢图（10-11 21:09 / 10-12 00:36）都是这个形状。**图其实已经在
    ComfyUI 的 output 目录里了**，重取一次就拿到，不必重跑生成。

    窗口取 180 秒：和 `_restart_comfy` 等它回来的上限同一量级（冷启动约 60 秒）。
    窗口用尽还有最后一招 `_copy_from_comfy_output`（磁盘兜底，需配
    `COMFYUI_OUTPUT_DIR`），都没有才抛最后一个异常。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    deadline = time.time() + _DOWNLOAD_RETRY_SECS
    last = None
    while True:
        try:
            resp = _session.get(COMFYUI_URL.rstrip("/") + "/view",
                               params={"filename": str(filename)}, timeout=120)
            resp.raise_for_status()
            with open(dst, "wb") as f:
                f.write(resp.content)
            return dst
        except Exception as e:
            last = e
            if time.time() >= deadline:
                break
            log.warning("取图 %s 失败（%s），ComfyUI 多半在重启，%d 秒后重试",
                        filename, type(e).__name__, _DOWNLOAD_RETRY_GAP)
            time.sleep(_DOWNLOAD_RETRY_GAP)
    got = _copy_from_comfy_output(filename, dst)
    if got:
        return got
    raise last


def _copy_from_comfy_output(filename, dst):
    """磁盘兜底：`/view` 一直取不到时，直接从 ComfyUI 的 output 目录复制同名文件。

    只在 `COMFYUI_OUTPUT_DIR` 配了、且确实有同名文件时才动手。**不做「取最新
    一张」那种猜测**——图名来自 ComfyUI 的 history，是 SaveImage 写下的真实
    名字，同名即同图；猜错会静默落一张别的图，比报错更难查。返回落盘路径，
    没得兜就返回 None（由调用方抛原异常）。
    """
    root = (COMFYUI_OUTPUT_DIR or "").strip()
    name = os.path.basename(str(filename))
    if not root or not name or name != str(filename):
        return None                      # 没配，或名字带路径成分 —— 一律不认
    src = os.path.join(root, name)
    if not os.path.isfile(src):
        return None
    shutil.copyfile(src, dst)
    log.warning("改从 ComfyUI output 目录直接取图：%s", src)
    return dst


def _submit_nai(prompt, skill):
    """把一张 NAI 图排进云端通道并等出图，返回**已经落盘的本地路径**。

    走 `image_jobs._process_nai`：target=None 时它把图存进 image_out 的目录，
    并把那个**路径字符串**塞进 `entry["images"]`——不是 ComfyUI 那种
    `{"filename": ...}` 字典，所以这里**不能**用 `output_images` 解析，也
    没有 `/view` 可下（图本来就在本机）。

    NAI 是云端出图，种子不可控（同 generate_image 里那条口径），所以不传 seed。
    """
    job, reason = image_jobs.enqueue(None, None, prompt, skill=skill,
                                     prompt=prompt, landscape=False)
    if reason:
        raise RuntimeError(reason)
    entry = job.wait()                       # 失败会抛 job.error
    paths = (entry or {}).get("images") or []
    if not paths:
        raise RuntimeError("跑完了但没找到输出图片")
    return paths[0]


def render_scene(task, p, scene_skill=SCENE_SKILL, opts=None):
    """阶段一：文生图，出这一格的场景图。

    渠道可以是本机（anima-line，ComfyUI）或云端（nai）——见模块顶部的说明。

    `opts`（2026-10-13）：这个任务的渲染参数覆盖（render.json：LoRA 槽 / 两段
    采样 / 分辨率）。None 时现场从任务目录读——CLI 直接调本函数也享受覆盖。
    只对本机渠道有意义（NAI 的参数在别人机器上，上面那个分支提前返回了）。
    """
    if scene_skill in image_jobs.NAI_SKILLS:
        log.info("[格 %s] 场景渲染（%s 云端，无种子）", p.no, scene_skill)
        src = _submit_nai(p.scene, scene_skill)
        os.makedirs(task.raw_dir, exist_ok=True)
        shutil.copyfile(src, task.raw_path(p))
        p.seed = None
        p.state = "rendered"
        p.error = None
        log.info("[格 %s] 场景已落盘：%s", p.no, task._rel(task.raw_path(p)))
        return

    if opts is None:
        opts = comic_render_opts.load(task.dir, scene_skill)
    seed = _new_seed()
    wf = generate_image.build_t2i_workflow(scene_skill, p.scene, seed)
    if wf is None:
        raise RuntimeError("skill %s 没有文生图工作流" % scene_skill)
    wf = comic_render_opts.apply(wf, opts)
    log.info("[格 %s] 场景渲染（%s，seed %s，%s）", p.no, scene_skill, seed,
             comic_render_opts.describe(opts))
    name = _submit_retry(lambda: _submit(wf, scene_skill, p.scene, seed),
                         "[格 %s] 场景" % p.no)
    _download(name, task.raw_path(p))
    p.seed = seed
    p.state = "rendered"
    p.error = None
    log.info("[格 %s] 场景已落盘：%s", p.no, task._rel(task.raw_path(p)))


def _upload_source(path):
    """把本地场景图缩到画布长边后送进 ComfyUI 的 input 目录，返回它认得的名字。"""
    with open(path, "rb") as f:
        raw = f.read()
    fitted, _size = comfy_src.fit(raw, max_side=comfy_src.MAX_SIDE)
    return comfy_src.upload(fitted)


def _build_i2i(skill, prompt, seed, uploaded, denoise=I2I_DENOISE):
    """构建图生图工作流。

    占位符替换**复用 generate_image 里那两个函数**，不另写一份——转义规则
    只该有一处（apply_prompt_placeholders 的注释专门交代过别让它分叉）。

    denoise 可覆盖：管线自己的两遍 i2i 都用 I2I_DENOISE(0.60)，而**手动改字**
    要的是「只重画那几个字、别动人物和已有气泡」，强度得能调低（见 render_manual）。
    """
    sd = load_skill(skill)
    if not sd:
        raise RuntimeError("找不到 skill %s" % skill)
    wf = load_workflow(os.path.join(sd["path"], "workflow_i2i.json"))
    if not wf:
        raise RuntimeError("skill %s 没有图生图工作流" % skill)
    wf = generate_image.apply_prompt_placeholders(json.dumps(wf), prompt, seed)
    return generate_image._apply_i2i_placeholders(wf, uploaded, denoise)


def render_bubble(task, p, split=False, prompt=None):
    """阶段二：qwen 图生图，在场景图上加这一格的对话框。

    有 `气泡2:` 的格**默认一遍就把两个气泡和旁白全画完**（BUBBLE_TEMPLATE_ALL，
    2026-10-11 合并；依据见那个模板的注释）。

    split=True 走旧的分开画：第一遍按 bubble_prompt(split=True) 画左上角气泡
    +旁白，第二遍拿第一遍的成图当源图、按 bubble2_prompt 补第二个气泡
    （BUBBLE_TEMPLATE_2 的数量约束是按「图里已有第一个气泡」写的）。这条留作
    退路——某格一遍画崩了，界面上把它单独切回「分开画」重跑。

    prompt 给了就**整段替换**第一遍的提示词（界面上的「完整提示词」框：用户
    要看见原样发给 qwen 的文字并改它）。此时 split 的第二遍照旧只由 split 决定
    ——用户在框里写的就是那一段的全部，不想有第二遍就把画法切回「一遍画完」。
    """
    raw_path = task.raw_path(p)
    if not os.path.exists(raw_path):
        raise RuntimeError("没有场景原图，先跑阶段一")
    overridden = bool((prompt or "").strip())
    if overridden:
        prompt = prompt.strip()
    else:
        prompt = p.bubble_prompt(split=split)
    seed = _new_seed()
    uploaded = _upload_source(raw_path)
    wf = _build_i2i(BUBBLE_SKILL, prompt, seed, uploaded)
    log.info("[格 %s] 加气泡（%s，seed %s%s%s）", p.no, BUBBLE_SKILL, seed,
             "，分开画" if split else "", "，提示词已手改" if overridden else "")
    name = _submit_retry(lambda: _submit(wf, BUBBLE_SKILL, prompt, seed),
                         "[格 %s] 加气泡" % p.no)
    _download(name, task.final_path(p))
    if split and p.two_bubbles():
        # 有旁白条时它占了右上角，第二个气泡让位到右下角（见 BUBBLE_TEMPLATE_2）
        pos = "右下角" if p.labels() else "右上角"
        prompt2 = p.bubble2_prompt(pos)
        seed2 = _new_seed()
        uploaded2 = _upload_source(task.final_path(p))   # 源图=第一遍的成图
        wf2 = _build_i2i(BUBBLE_SKILL, prompt2, seed2, uploaded2)
        log.info("[格 %s] 加第二个气泡（%s，seed %s）", p.no, BUBBLE_SKILL, seed2)
        name2 = _submit_retry(lambda: _submit(wf2, BUBBLE_SKILL, prompt2, seed2),
                              "[格 %s] 加第二个气泡" % p.no)
        _download(name2, task.final_path(p))             # 覆盖成两气泡版
    p.state = "done"
    p.error = None
    log.info("[格 %s] 成品已落盘：%s", p.no, task._rel(task.final_path(p)))


# 手动修复时自动追加的约束（用户 2026-10-11 拍板：指令原样在前、约束自动在后）。
# qwen 图生图不写这句，常会顺手把整张画重画——只想改一个字却把人物也改了，
# 比不改还糟。
MANUAL_KEEP_NOTE = "保持人物、服装、姿势、背景和画风完全不变，只改我说的这部分。"


def _manual_prompt(instruction):
    """手输指令 → 喂给 qwen 的最终提示词。"""
    return "%s %s" % ((instruction or "").strip(), MANUAL_KEEP_NOTE)


def render_manual(task, p, instruction, denoise=I2I_DENOISE):
    """手动修复：拿这一格的**当前成品**当源图，按用户手输的指令再跑一遍 qwen。

    和 render_bubble 的关键差别只有一条：源图是 `final/`（已经画上字的成图），
    不是 `raw/` 底图。所以这一遍是**累积**的——用户看到哪几个字糊了就说
    「把 X 改成 Y」，qwen 在图生图上就地重画那几处。

    按累积来设计是照用户的实际用法：2026-10-11 原话「就会基于最后的加了字幕的
    完整底图，我会告诉 qwen 某某字很模糊，把这个字重新变成 XX 字」。改坏了有
    回退路——`/api/comic/bubble` 拿剧本提示词从**底图**重画，底图全程不动。

    还没有成品（只有底图）时自动退到底图当源图，这样手动入口对「待加气泡」的
    格也能用。
    """
    _require_comfy()
    src = task.final_path(p)
    if not os.path.exists(src):
        src = task.raw_path(p)
    if not os.path.exists(src):
        raise RuntimeError("这一格还没有任何图（底图都没出），先跑阶段一")
    prompt = _manual_prompt(instruction)
    seed = _new_seed()
    uploaded = _upload_source(src)
    wf = _build_i2i(BUBBLE_SKILL, prompt, seed, uploaded, denoise)
    log.info("[格 %s] 手动修复（%s，seed %s，强度 %s）",
             p.no, BUBBLE_SKILL, seed, denoise)
    name = _submit_retry(lambda: _submit(wf, BUBBLE_SKILL, prompt, seed),
                         "[格 %s] 手动修复" % p.no)
    # 先落临时名再原子替换：这一遍是「在成品上改」，写到一半坏掉等于把上一版
    # 好成品丢了，用户没得退。
    tmp = task.final_path(p) + ".tmp"
    _download(name, tmp)
    os.replace(tmp, task.final_path(p))
    p.state = "done"
    p.error = None
    task.save_state()
    log.info("[格 %s] 手动修复已落盘：%s", p.no, task._rel(task.final_path(p)))


# ─── 跑批 ─────────────────────────────────────────────

def _next_task_dir(root="comics"):
    """按 `C<日期>-<序号>` 找第一个还没被占用的任务目录名。"""
    day = datetime.now().strftime("%Y%m%d")
    n = 1
    while os.path.exists(os.path.join(root, "C%s-%02d" % (day, n))):
        n += 1
    return os.path.join(root, "C%s-%02d" % (day, n))


def run(script_path, only=None, silver_only=False, redo=False,
        scene_skill=SCENE_SKILL, split_bubbles=False, scene_opts=None):
    meta, panels = parse_script(script_path)
    if not panels:
        raise SystemExit("剧本里没解析到任何格子（每格以 `## 01` 这样的标题开头）")

    task = Task(script_path, meta, panels)
    task.load_state()
    task.save_state()
    # 渲染参数覆盖（render.json）开跑时读一次，整批用同一套——跑到一半改参数
    # 不会这批用新、下批用旧。None = 这个任务没存过，render_scene 那边按模板
    # 默认值走。NAI 云端不读（参数在别人机器上，见 render_scene）。
    if scene_opts is None and scene_skill not in image_jobs.NAI_SKILLS:
        scene_opts = comic_render_opts.load(task.dir, scene_skill)
    # 阶段一走 NAI 云端、又只要阶段一 → 完全不碰 ComfyUI，不用等它在线；
    # 其余情况（本机 silver，或还要跑阶段二 qwen）都得先确认 ComfyUI 在线。
    if not (silver_only and scene_skill in image_jobs.NAI_SKILLS):
        _require_comfy()
    log.info("任务 %s：%d 格，目录 %s（阶段一渠道 %s）",
             task.task_id, len(panels), task.dir, scene_skill)

    todo = [p for p in task.panels if not only or p.no in only]

    if redo:            # 强制重跑选定格的阶段二（场景图留着复用，只重加气泡）
        for p in todo:
            if p.state == "done":
                p.state = "rendered"
                p.error = None

    # 阶段一：所有格先用同一个渠道渲染完（按模型分批，别逐格换渠道）
    for p in todo:
        if p.state in ("rendered", "done"):
            log.info("[格 %s] 场景已就绪，跳过", p.no)
            continue
        try:
            render_scene(task, p, scene_skill, opts=scene_opts)
        except Exception as e:                       # 单格失败不拖垮整批
            p.error = str(e)
            log.error("[格 %s] 场景渲染失败：%s", p.no, e)
        task.save_state()

    if silver_only:
        _report(task)
        return task

    # 阶段二：统一换 qwen 加气泡
    for p in todo:
        if p.state == "done":
            log.info("[格 %s] 已完成，跳过", p.no)
            continue
        if p.state != "rendered":
            log.warning("[格 %s] 场景没出图，跳过加气泡", p.no)
            continue
        try:
            render_bubble(task, p, split=split_bubbles)
        except Exception as e:
            p.error = str(e)
            log.error("[格 %s] 加气泡失败：%s", p.no, e)
        task.save_state()

    _report(task)
    return task


def _report(task):
    print("\n—— %s 进度 ——" % task.task_id)
    for p in task.panels:
        line = "  格 %s  %s" % (p.no, _STATE_LABEL.get(p.state, p.state))
        if p.seed is not None:
            line += "  seed=%s" % p.seed
        if p.label:
            # 旁白可以有多条，命令行里挤成一行看
            line += "  标签=%s" % " / ".join(p.labels())
        if p.state == "done":
            line += "  → %s" % task._rel(task.final_path(p))
        elif p.state == "rendered":
            line += "  → %s" % task._rel(task.raw_path(p))
        if p.error:
            line += "  ✗ %s" % p.error
        print(line)


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m app.comic", description="漫画工作流调度器")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="跑一个剧本")
    r.add_argument("script", help="剧本 md 的路径")
    r.add_argument("--only", help="只跑这些格，逗号分隔，如 01,02")
    r.add_argument("--silver-only", action="store_true",
                   help="只跑阶段一（场景图），不加气泡")
    r.add_argument("--scene-skill", default=SCENE_SKILL,
                   help="阶段一的生图渠道，默认 %s；传 nai 走云端（快，"
                        "验证气泡效果时用）" % SCENE_SKILL)
    r.add_argument("--redo", action="store_true",
                   help="强制重跑选定格的阶段二（加气泡），场景图复用")
    r.add_argument("--split-bubbles", action="store_true",
                   help="有第二句台词的格分两遍画（默认一遍画完两个气泡；"
                        "某格一遍画崩了再用这个单独重跑）")

    s = sub.add_parser("status", help="看进度")
    s.add_argument("script", help="剧本 md 的路径")

    # 编剧层（app/comic_writer.py）：把一句话设定变成上面那些命令要的剧本。
    w = sub.add_parser("write", help="只写剧本，不跑图")
    w.add_argument("brief", help="一句话设定（剧情要什么）")
    w.add_argument("--panels", type=int, default=2, help="格数，默认 2（反转结构）")
    w.add_argument("--character", default="",
                   help="角色外观英文标签（可选，写了就固定用它）")
    w.add_argument("--out", required=True,
                   help="剧本写到哪，如 comics/Cxxx/script.md")

    n = sub.add_parser("new", help="写剧本并直接开跑")
    n.add_argument("brief", help="一句话设定（剧情要什么）")
    n.add_argument("--panels", type=int, default=2, help="格数，默认 2（反转结构）")
    n.add_argument("--character", default="",
                   help="角色外观英文标签（可选，写了就固定用它）")
    n.add_argument("--scene-skill", default=SCENE_SKILL,
                   help="阶段一的生图渠道，默认 %s" % SCENE_SKILL)
    n.add_argument("--silver-only", action="store_true",
                   help="只跑阶段一（场景图），不加气泡")

    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")

    if args.cmd == "status":
        meta, panels = parse_script(args.script)
        task = Task(args.script, meta, panels)
        task.load_state()
        _report(task)
        return 0

    if args.cmd in ("write", "new"):
        # 函数内 import：comic_writer 反过来要 import comic（复用解析器），
        # 模块级互相 import 会成环。
        from app import comic_writer
        text = comic_writer.write_script(args.brief, panels=args.panels,
                                         character=args.character)
        if args.cmd == "write":
            path = os.path.abspath(args.out)
        else:
            path = os.path.join(_next_task_dir("comics"), "script.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(text)                       # 让用户先看到剧本长什么样
        log.info("剧本已写入 %s", path)
        if args.cmd == "write":
            return 0
        run(path, silver_only=args.silver_only, scene_skill=args.scene_skill)
        return 0

    only = None
    if args.only:
        only = {x.strip().zfill(2) for x in args.only.split(",") if x.strip()}
    run(args.script, only=only, silver_only=args.silver_only, redo=args.redo,
        scene_skill=args.scene_skill, split_bubbles=args.split_bubbles)
    return 0


if __name__ == "__main__":
    sys.exit(main())