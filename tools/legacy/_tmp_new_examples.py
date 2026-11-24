# -*- coding: utf-8 -*-
import json
import re
from pathlib import Path

root = Path(r'D:\AI\my_agent_restructure')
skill = Path(r'D:\AI\agent_my_test\skills\writing\04-liveness\refs-人味\examples.md')


def sections():
    text = skill.read_text(encoding='utf-8')
    parts = re.split(r'\n(?=## )', text)
    return [p.strip() for p in parts if p.startswith('## 示例 ')]


def pair(path, user_i, assistant_i):
    rows = [json.loads(l) for l in Path(path).read_text(encoding='utf-8').splitlines() if l.strip()]
    user = rows[user_i]['content'].strip()
    raw = rows[assistant_i]['content'].strip()
    body = re.split(r'\n(?:画面|服饰|心情|状态)：', raw)[0].strip()
    return user, body


sagi = r'D:\AI\my_agent_restructure\agents\qq\sessions\persona_web_2509355624__izumi_sagiri.jsonl'
archive = r'D:\AI\my_agent_restructure\agents\qq\sessions\persona_archive_web_3478245189__izumi_sagiri.jsonl'
archive2 = r'D:\AI\my_agent_restructure\agents\qq\sessions\persona_archive_web_2139113764__izumi_sagiri.jsonl'

pairs = [
    ('一 · 催她去睡', sagi, 0, 1),
    ('二 · 还不想睡', sagi, 2, 3),
    ('三 · 怕黑被戳穿', sagi, 4, 5),
    ('四 · 让她自己上床', sagi, 6, 7),
    ('五 · 真的上床', sagi, 8, 9),
    ('六 · 关门晚安', sagi, 10, 11),
    ('七 · 用她的杯子喝水', archive, 22, 23),
    ('八 · 椅子被占了', archive2, 34, 35),
    ('九 · 好好抱一下', archive2, 52, 53),
    ('十 · 原来是想抱我', archive2, 18, 19),
]

real = ['## 真实回合示范（从现有存档原文抽取，不改写）\n']
for title, path, ui, ai in pairs:
    u, a = pair(path, ui, ai)
    real.append('### %s\n\n玩家：%s\n\n角色：%s\n' % (title, u, a))

fixed = ['## 改前 / 改后（去 AI 味 skill 原文）\n']
fixed.extend(sections())

body = '\n'.join(real) + '\n' + '\n'.join(fixed)

module = '''# -*- coding: utf-8 -*-
"""正文示范：真实回合 + 去 AI 味 skill 的改前/改后原文。

当前版本不再放角色卡开场白。开场白没有「玩家说一句 → 角色怎么接」的
因果，模型只能学到静态描写，学不到回合该停在哪里。

真实回合来自本机现有存档，原文抽取，只去掉正文后的画面/服饰/心情/状态四行；
人物、句子都没有改写。
改前/改后来自 skills/writing/04-liveness/refs-人味/examples.md，原文搬入，
用来教「这里该用什么」，不是教「不要用什么」。
"""

import re

EXAMPLE = """%s"""

_HE = re.compile(r"(?<!其)他(?!们|人)")


def full_example():
    """返回带标题的完整示范块；标题只出现一次。"""
    return (
        "## 完整示范（真实回合 + 改前改后，学怎么接住一句话）\\n\\n"
        "先看真实回合：玩家说一句，角色只接这一句，停在该停的地方。"
        "再看改前/改后：每一条都告诉你，AI 腔的句子换成什么写法。\\n\\n"
        + EXAMPLE
    )


def prompt_example():
    """送进系统头前把指代玩家的「他」机械纠成「你」。

    示范原文不改：存档里她心里说「他听见了」是真实口吻；但系统头里
    不能留「他」，否则模型会照着学。这里只做人称适配，不重写句子。
    """
    text = _HE.sub("你", full_example())
    return text.replace("他们", "别人").replace("他人", "别人")
''' % body

out = root / 'app' / 'features' / 'persona' / 'writing_examples.py'
out.write_text(module, encoding='utf-8', newline='\n')
print('chars', len(body))
for title, path, ui, ai in pairs:
    u, a = pair(path, ui, ai)
    print(len(u), len(a), title)
print('示例字符', sum(len(x) for x in sections()))
