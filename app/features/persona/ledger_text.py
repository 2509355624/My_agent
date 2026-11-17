"""账本与小记的文本压缩：按段/按节截断、第三人称归一、格式校验。

只做字符串处理，不读写状态。
"""

import re


def _cut_sentence(text, limit):
    """截到 `limit` 以内，尽量停在句末标点上（不切半句）。"""
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    head = raw[:limit]
    cut = -1
    for ch in "。！？…\n":
        cut = max(cut, head.rfind(ch))
    if cut >= limit * 0.5:
        return head[:cut + 1].rstrip()
    return head.rstrip() + "…"


def _clip_note(text, limit=1000):
    """把过长的小记压进 `limit`：**保开头也保结尾，丢中间**。

    2026-11-04 修：原来 `note[:limit]` 硬切，而小记是按时序写的——最新的
    在末尾，一超长就被砍掉的正好是刚发生的事（实测这条线连续 7 次压缩都是
    「正好 1000 字」，全切在最新那段之前）。结果就是「回忆」永远停在上一次，
    模型手里既没有窗口也没有小记，只能凭句式瞎猜谁做了什么。

    开头留着是因为那是关系的基础，结尾留着是因为那是刚发生的事，中间的过渡
    细节最不值钱——小记本来就是摘要，不是流水账。

    按段落取舍，段内再按句末标点收尾；段落太少（一整块）时退化成按句截尾。
    """
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    paras = [p.strip() for p in s.replace("\r", "").split("\n") if p.strip()]
    if len(paras) < 3:
        return _cut_sentence(s, limit)
    # 尾部拿七成（2026-11-04 实测：模型根本不理「900 字以内」，这次直接写了
    # 2002 字）。既然注定要丢，就丢旧的——玩家能感觉到「她不记得刚才」，
    # 但感觉不到「她记不清上周」。
    head_budget = int(limit * 0.3)
    tail_budget = limit - head_budget - 6          # 6 给中间的「……」
    head, used = [], 0
    for p in paras:
        if used + len(p) + 1 > head_budget:
            break
        head.append(p)
        used += len(p) + 1
    if not head:                                   # 第一段就超预算
        return _cut_sentence(s, limit)
    tail, used = [], 0
    for p in reversed(paras[len(head):]):
        if used + len(p) + 1 > tail_budget:
            break
        tail.insert(0, p)
        used += len(p) + 1
    if not tail:
        return _cut_sentence("\n".join(head), limit)
    return "\n".join(head) + "\n……\n" + "\n".join(tail)


# 账本的六个固定小节（2026-11-05）。顺序也是注入顺序：关系 → 你做的事 →
# 她做的事 → 约定 → 忌讳 → 日常。标题不要求一字不差，只要包含关键词就算
# 这一节在——模型偶尔会给标题补半句解释，不能因此把整份账本判废。
# 2026-11-06：「我的事」改成「她的事」——账本统一第三人称，和正文同一套指代。
_LEDGER_SECTIONS = ("关系", "你的事", "她的事", "约定", "忌讳", "日常")

# 账本里指代玩家的第三人称（2026-11-06 用户报「上下文算他的」）。
# 「其他 / 他们 / 他人」不是指代对方，不算违规。
_PRONOUN_HE = re.compile(r"(?<!其)他(?!们|人)")
# 引号内是她引用的原话，那里的「我」必须保留；只有引号外才改成「她」。
# 外层括号是给 `split` 保留分隔符用的：没有捕获组，split 会把引号内容一起吞掉。
_QUOTED = re.compile(
    r"(「[^」]*」|『[^』]*』|“[^”]*”|\"[^\"]*\")")
# 「我们」是她和玩家的合称，不能拆成「她们」。
_PRONOUN_WO = re.compile(r"我(?!们)")


def _ledger_fix_pronouns(text):
    """把账本统一成第三人称：女主角=「她」，玩家=「你」。

    账本曾经用第一人称写（「我的事」），和正文的第三人称混在一起，模型
    分不清「我」是谁（2026-11-06 用户报「算他的」）。现在账本和正文用同一套
    指代，存量脏账本在读入/再压缩前先过这一遍：
      · 引号外：指代玩家的「他」→「你」；女主角的「我」→「她」；
      · 引号内：她说的原话不动（「我」仍然是她自己）。
    只改人称，不改事实。
    """
    out = []
    for part in _QUOTED.split(str(text or "")):
        if part and _QUOTED.fullmatch(part):
            out.append(part)
        else:
            out.append(_PRONOUN_WO.sub("她", _PRONOUN_HE.sub("你", part)))
    return "".join(out)


def _ledger_ok(text):
    """这份输出像不像一份账本：至少认出四节，否则保留旧账本。

    2026-11-06：再加一道人称硬校验——整份账本里只要出现指代玩家的「他」，
    就整份拒收。宁可保留旧账本，也不能把「他」喂给演员：她读到「他」，
    下一轮就会把「算我的」说成「算他的」。
    """
    raw = str(text or "")
    if _PRONOUN_HE.search(raw):
        return False
    heads = re.findall(r"^[ \t]*#{1,3}[ \t]*(.+)$", raw, re.M)
    if len(heads) < 4:
        return False
    hit = 0
    for key in _LEDGER_SECTIONS:
        if any(key in h for h in heads):
            hit += 1
    return hit >= 4


def _clip_ledger(text, limit=1000):
    """账本超长时**按小节截**，不按开头/结尾截。

    `_clip_note` 是给散文用的（保头保尾、丢中间）。账本的中间是「他的事 /
    我的事 / 约定」，丢哪一节都会让下一轮失忆，所以这里按节平分预算，
    每节内部再按句收尾，**六个标题都留着**。
    """
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    blocks, cur = [], []
    for line in s.splitlines():
        if re.match(r"^[ \t]*#{1,3}[ \t]*", line) and cur:
            blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)
    if cur:
        blocks.append(cur)
    if len(blocks) < 2:
        return _clip_note(s, limit)
    heads = [b[0].strip() for b in blocks]
    room = limit - sum(len(h) + 1 for h in heads) - len(blocks)
    body_budget = max(12, room // len(blocks))
    out = []
    for b in blocks:
        head = b[0].strip()
        body = "\n".join(b[1:]).strip()
        if len(body) > body_budget:
            body = _cut_sentence(body, body_budget)
        out.append(head + ("\n" + body if body else ""))
    res = "\n".join(out).strip()
    return res if len(res) <= limit else res[:limit].rstrip()
