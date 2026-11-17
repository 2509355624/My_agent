# -*- coding: utf-8 -*-
"""角色点名守卫（2026-10-04，胡桃桃私聊实测拍板）。

实测：用户说「画纳西妲」，MiMo 把历史里出现频率最高的胡桃模板整段
搬出来（连角色名都不换）；先试了正向规则（image_guide「新请求=全新
提示词」），热加载确认规则进了模型，照抄不误——和 9B 时代的负向规则
同一个病：**描述压不住高频锚定，判据得代码判**。

判据是客观的、精度高：本轮原话点名了角色 A，prompt 里却写着角色 B
（且没有 A）→ 拒回并点名要求重写。模型在 agent 循环里拿着错误信息
重试，守卫不替模型写提示词，只判「点名与产出对不对得上」。

两个刻意的放行（宁可漏判不可误判）：
- prompt 里没有任何可识别角色 → 放行（image_guide 教过「没把握就写
  外貌+作品名」，那是合规路子，不是这次要治的病）；
- 点名的角色全都在 prompt 里 → 放行（多人图 / 换装续画都算对题）。
"""
import logging
import re

log = logging.getLogger(__name__)

# 别名表：canonical（小写，会被当 prompt 侧角色 tag 匹配）-> 用户侧叫法。
# 中文名用子串匹配，英文名用户侧也用子串；prompt 侧对 ASCII canonical
# 用词边界正则（防止 jeans 撞 jean 这类）。_NO_PROMPT_MATCH 里的英文词
# 同时是常用词（amber eyes / jean jacket / gaming），只在用户侧生效，
# 不参与 prompt 侧判定。
_CHARS = {
    "hu tao": ["胡桃", "hutao"],
    "nahida": ["纳西妲", "纳西达"],
    "kamisato ayaka": ["神里绫华", "神里凌华", "绫华", "ayaka"],
    "xiangling": ["香菱"],
    "raiden shogun": ["雷电将军", "雷神", "raiden"],
    "zhongli": ["钟离"],
    "ganyu": ["甘雨", "椰羊"],
    "keqing": ["刻晴"],
    "eula": ["优菈", "优拉"],
    "shenhe": ["申鹤"],
    "yelan": ["夜兰"],
    "nilou": ["妮露"],
    "yae miko": ["八重神子", "yae"],
    "yoimiya": ["宵宫"],
    "mona": ["莫娜"],
    "barbara": ["芭芭拉"],
    "fischl": ["菲谢尔", "皇女"],
    "noelle": ["诺艾尔", "诺埃尔"],
    "diluc": ["迪卢克"],
    "kaeya": ["凯亚"],
    "venti": ["温迪"],
    "klee": ["可莉"],
    "tartaglia": ["达达利亚", "公子", "childe"],
    "kaedehara kazuha": ["枫原万叶", "万叶", "kazuha"],
    "sangonomiya kokomi": ["珊瑚宫心海", "心海", "kokomi"],
    "kujou sara": ["九条裟罗", "kujou"],
    "arataki itto": ["荒泷一斗", "一斗", "itto"],
    "yun jin": ["云堇"],
    "kuki shinobu": ["久岐忍", "shinobu"],
    "shikanoin heizou": ["鹿野院平藏", "平藏", "heizou"],
    "kirara": ["绮良良"],
    "baizhu": ["白术"],
    "wriothesley": ["莱欧斯利"],
    "neuvillette": ["那维莱特"],
    "lyney": ["林尼"],
    "lynette": ["林妮特"],
    "freminet": ["菲米尼"],
    "kaveh": ["卡维"],
    "wanderer": ["流浪者", "散兵"],
    "furina": ["芙宁娜", "芙卡洛斯"],
    "charlotte": ["夏洛蒂"],
    "chevreuse": ["夏沃蕾"],
    "xianyun": ["闲云"],
    "gaming": ["嘉明"],
    "arlecchino": ["阿蕾奇诺"],
    "clorinde": ["克洛琳德"],
    "sigewinne": ["希格雯"],
    "emilie": ["艾梅莉埃"],
    "chiori": ["千织"],
    "alhaitham": ["艾尔海森"],
    "cyno": ["赛诺"],
    "dehya": ["迪希雅"],
    "layla": ["莱依拉"],
    "faruzan": ["珐露珊"],
    "collei": ["柯莱"],
    "yaoyao": ["瑶瑶"],
    "dori": ["多莉"],
    "candace": ["坎蒂丝"],
    "xilonen": ["希诺宁"],
    "mualani": ["玛拉妮"],
    "kinich": ["基尼奇"],
    "kachina": ["卡齐娜"],
    "mavuika": ["玛薇卡"],
    "citlali": ["茜特菈莉"],
    "ororon": ["欧洛伦"],
    "chasca": ["恰斯卡"],
    "varesa": ["瓦雷莎"],
    "lan yan": ["蓝砚"],
    "iansan": ["伊安珊"],
    "amber": ["安柏"],
    "jean": ["琴团长"],
    "albedo": ["阿贝多"],
    "rosaria": ["罗莎莉亚"],
    "yanfei": ["烟绯"],
    "beidou": ["北斗"],
    "ningguang": ["凝光"],
    "xinyan": ["辛焱"],
    "chongyun": ["重云"],
    "xingqiu": ["行秋"],
    "qiqi": ["七七"],
    "diona": ["迪奥娜"],
    "sucrose": ["砂糖"],
    "bennett": ["班尼特"],
    "razor": ["雷泽"],
    "xiao": ["魈"],
}
# 这些 canonical 是常用英文词，prompt 侧不拿它们当角色证据（amber eyes、
# jean jacket、gaming、wanderer 之类会误伤）。用户侧照常生效。
_NO_PROMPT_MATCH = {"amber", "jean", "gaming", "wanderer", "charlotte", "layla"}

# 别名 -> canonical，构建一次。别名按长度降序排，长词优先命中
# （「神里绫华」先于「绫华」，避免短别名抢走）。
_ALIAS2CANON = []
for _canon, _aliases in _CHARS.items():
    for _a in _aliases:
        _ALIAS2CANON.append((_a.lower(), _canon))
_ALIAS2CANON.sort(key=lambda x: -len(x[0]))

_ASCII_CANON_RE = {}
for _canon in _CHARS:
    if all(ord(c) < 128 for c in _canon) and _canon not in _NO_PROMPT_MATCH:
        _ASCII_CANON_RE[_canon] = re.compile(
            r"\b" + re.escape(_canon) + r"\b")


def _mentioned(text):
    """本轮原话点名的角色（canonical 列表，保序去重）。"""
    t = (text or "").lower()
    if not t:
        return []
    out = []
    for alias, canon in _ALIAS2CANON:
        if alias in t and canon not in out:
            out.append(canon)
    return out


# 展示名：报错文案里给用户侧的叫法（第一个含中文的别名），没有中文
# 别名的用 canonical 本身——「你点的是【纳西妲】」比「nahida」好读。
_DISPLAY = {}
for _canon, _aliases in _CHARS.items():
    _zh = [a for a in _aliases if any(ord(c) > 127 for c in a)]
    _DISPLAY[_canon] = _zh[0] if _zh else _canon

# 中文 prompt 侧识别：qwen 渠道写中文句子，角色可能直接写「胡桃」。
# 拿各角色的中文别名做子串匹配（只含 CJK 的别名；英文别名在中文句里
# 没意义）。误伤面小：只在用户点名了角色时才走到这一步。
_CJK_ALIAS_RE = {}
for _canon, _aliases in _CHARS.items():
    _cjk = [a for a in _aliases if all(ord(c) > 127 for c in a)]
    if _cjk:
        _CJK_ALIAS_RE[_canon] = re.compile("|".join(
            re.escape(a) for a in sorted(_cjk, key=len, reverse=True)))


def _present_in_prompt(prompt):
    """prompt 里能认出来的角色集合（英文词边界 + 中文别名子串）。"""
    p = (prompt or "").lower()
    found = set()
    for canon, cre in _ASCII_CANON_RE.items():
        if cre.search(p):
            found.add(canon)
    for canon, cre in _CJK_ALIAS_RE.items():
        if canon not in found and cre.search(p):
            found.add(canon)
    return found


def check(prompt):
    """QQ 轮守卫入口：点名角色与 prompt 对不上时返回拒绝文案，否则 None。

    与守卫家族同判据：`current_turn_text()` 为 None（网页端/单测直调）
    一律放行。宁可漏判（对不上时模型照画）不可误判（白拦一轮）。
    """
    from app import qq_api
    text = qq_api.current_turn_text()
    if text is None:
        return None
    mentioned = _mentioned(text)
    if not mentioned:
        return None
    present = _present_in_prompt(prompt)
    missing = [c for c in mentioned if c not in present]
    if not missing:
        return None
    if not present:
        # prompt 里没有任何已知角色：可能是按外貌写的合规路子，放行。
        return None
    log.info("角色点名守卫拦下：原话点名 %s，prompt 里写的是 %s",
             missing, sorted(present))
    miss_zh = "、".join(_DISPLAY[c] for c in missing)
    got_zh = "、".join(_DISPLAY[c] for c in sorted(present))
    return (
        "错误：用户本轮点名的是【%s】，你的提示词里写的却是【%s】——"
        "整条提示词是从上一轮抄来的。重写一遍：把角色名、发型、发色、"
        "瞳色、头饰、服装全部换成【%s】的，上一轮提示词里用户没有明确"
        "要求继承的槽位一并按新角色重新填；写完重新调用本工具。"
        % (miss_zh, got_zh, miss_zh))
