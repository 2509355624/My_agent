# -*- coding: utf-8 -*-
"""时段六档 + 状态注入的验收（不调模型、不改真实会话）。

    D:\\AI\\confyui_env\\Scripts\\python.exe tools\\_verify_phase_state.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import persona  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("%-44s %s %s" % (name, "✓" if ok else "✗", detail))
    if not ok:
        fails.append(name)


# 1) 时段归一
CASES = [("傍晚", "傍晚"), ("晚上", "傍晚"), ("黄昏", "傍晚"), ("清晨", "早晨"),
         ("午后", "下午"), ("半夜", "深夜"), ("傍晚前后", "傍晚"), ("乱写", "乱写")]
for raw, want in CASES:
    got = persona.normalize_phase(raw)
    check("normalize %s" % raw, got == want, got)

check("PHASES 六档", persona.PHASES == ("早晨", "上午", "中午", "下午", "傍晚", "深夜"),
      str(persona.PHASES))
check("傍晚 index=4", persona.phase_index("傍晚") == 4)

# 2) 前端按钮与后端同表
html = open(os.path.join("web", "persona.html"), encoding="utf-8").read()
btns = re.findall(r'data-phase="([^"]+)"', html)
check("前端按钮 = PHASES", tuple(btns) == persona.PHASES, str(tuple(btns)))
check("前端有灰显逻辑", "btn.past" in html and "btn.now" in html)
check("前端防重开", "SKIP_CLOSED_AT" in html)

# 3) 状态注入（两条路径都算过：有全书走锚点，没全书走内置档位）
from app import writing_skills  # noqa: E402
BOOK = bool(writing_skills.state_guide())
for ar, key in ((10, "calm"), (35, "mild"), (55, "warm"),
                (78, "hot"), (95, "burning")):
    blk = persona.body_state_block(
        {"active": True, "excite": 78, "arousal": ar, "value": 30,
         "corrupt": 0, "submit": 0, "suspicion": 0, "level": 2},
        {"day": 0, "phase": "深夜"})
    if BOOK:
        ok = ("## 你现在在" in blk and "高潮 78" in blk
              and ("冲动 %d" % ar) in blk and "组合命中" in blk)
    else:
        ok = ("高潮 78/100" in blk and ("冲动 %d/100" % ar) in blk
              and "### " in blk)
    check("body_state_block arousal=%d" % ar, ok,
          blk.split("\n")[0] if blk else "")

# 4) 五档都有词条（配置 key 与 _AROUSAL_LAYERS 对得上）
try:
    from app import affection
    keys = {str(x.get("key")) for x in affection.arousal_levels()}
    missing = keys - set(persona._AROUSAL_LAYERS)
    check("冲动档位全覆盖", not missing, str(missing))
except Exception as e:                                     # noqa: BLE001
    check("冲动档位全覆盖", False, repr(e))

# 5) 接触位置状态机（2026-11-07）
for raw, want in (("小穴", "pussy"), ("小穴里", "pussy"), ("后庭", "anus"),
                  ("肛门里", "anus"), ("嘴里", "mouth"), ("无", "none"),
                  ("手指", "hand"), ("乱写", "")):
    got = persona.normalize_contact(raw)
    check("contact %s" % raw, got == want, got or "(空)")

blk = persona.contact_block({"contact": "pussy", "contact_prev": "anus"})
check("换位置有过渡约束", "必须写清怎么退出来" in blk and "后庭" in blk)
blk = persona.contact_block({"contact": "mouth", "contact_prev": "none"})
check("首次进入有提醒", "第一次进入" in blk)
blk = persona.contact_block({"contact": "pussy", "contact_prev": "pussy"})
check("同位置不加约束", "⚠" not in blk)
check("无接触返回空", persona.contact_block({"contact": "", "contact_prev": "anus"}) == "")

# 6) 没状态时不能硬塞
check("无 active 返回空", persona.body_state_block({"active": False}) == "")
check("None 返回空", persona.body_state_block(None) == "")

print("\n失败 %d" % len(fails), fails)
sys.exit(1 if fails else 0)
