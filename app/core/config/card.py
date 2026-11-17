"""角色卡配置域：内置卡保护名单。

名单同时被扮演态（persona）和造卡页（card_maker）读取；这里只放名单，
卡库文件路径与读写逻辑在 app/card_maker.py、app/persona.py。
"""

# ─── 内置角色卡（2026-11-03 用户要求「创建角色不能动我的默认角色」）──────
#
# 这些 id 对应的卡是随项目一起长出来的（改过人设、定过年龄），用户明确说
# 不能改、不能删。
#
# 两个卡库都要认这份名单：
#   · `persona`（扮演态）→ `agents/qq/characters.json`
#   · `card_maker`（造卡页）→ `state/xingyun_cards.json`
#
# 除了这份名单，卡上带 `"builtin": true` 也算内置——以后想保护新卡，
# 改 json 就行，不用改代码。等全部卡都补上那个字段，这份名单就可以删。
BUILTIN_CARD_IDS = frozenset({
    "izumi_sagiri", "rem", "asuna", "1787749573788", "sister",
    "sister_friend", "childhood_friend", "junior", "senior",
    "big_sister", "mature_woman",
})

__all__ = [name for name in globals()
           if name.isupper() and not name.startswith("_")]
