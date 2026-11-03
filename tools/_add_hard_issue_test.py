"""把 HardIssueTest 追加到 tests/test_moss_bot.py（一次性脚本）。"""
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tests", "test_moss_bot.py")

NEW = '''

class HardIssueTest(unittest.TestCase):
    """硬伤闸门（2026-11-03）：决定要不要补正一遍。

    用户拍板：单次调用没有 agent 那种「第二轮自我纠错」的回路，所以加一道
    条件补正。判据三类：结构（格数/景别/动作覆盖）、文字（气泡旁白超字）、
    套路化（景别顺序每页一样）。
    """

    GOOD_SHOTS = ["wide shot", "close-up", "extreme close-up",
                  "medium shot", "medium close-up"]

    def _base(self, shots=None, panels=None):
        return {
            "title": "t", "style": "warm", "cast": {"woman": "1girl"},
            "pages": [{"n": 1, "layout": "1t2m2b",
                       "plan": {"shots": shots or list(self.GOOD_SHOTS)},
                       "panels": panels or [
                           {"desc": "a " + "b " * 30} for _ in range(5)]}],
        }

    def test_catches_broken_spec(self):
        bad = self._base(
            shots=["close-up", "close-up", "close-up"],
            panels=[
                {"desc": "a close-up of her sitting"},
                {"desc": "a close-up of her sitting",
                 "bubbles": ["这句话明显超过十一个字了要拆开"]},
                {"desc": "a close-up"},
            ])
        probs = moss_comic.hard_issues(bad)
        msgs = " ".join(m for _, m in probs)
        self.assertIn("要 5 格", msgs)        # 格数不对
        self.assertIn("景别只有", msgs)        # 景别档数不足
        self.assertIn("超 11", msgs)          # 气泡超字

    def test_clean_spec_has_no_hard_issue(self):
        self.assertEqual(moss_comic.hard_issues(self._base()), [])

    def test_action_coverage_is_reported(self):
        probs = moss_comic.hard_issues(self._base(), "掰开她的小穴")
        self.assertTrue(any("掰开" in m for _, m in probs))

    def test_action_coverage_accepts_common_phrasings(self):
        """「舔」写成 mouth pressing / between her thighs 也算画到。"""
        spec = self._base(panels=[
            {"desc": "a man is between her thighs, mouth pressing"},
        ] + [{"desc": "a " + "b " * 30} for _ in range(4)])
        probs = moss_comic.hard_issues(spec, "舔")
        self.assertFalse(any("舔" in m for _, m in probs))
'''

MARK = '\n\nif __name__ == "__main__":'

with open(PATH, encoding="utf-8") as f:
    raw = f.read()
if "class HardIssueTest" in raw:
    raise SystemExit("已经追加过了")
if MARK not in raw:
    raise SystemExit("找不到插入点")
raw = raw.replace(MARK, NEW + MARK, 1)
with open(PATH, "w", encoding="utf-8") as f:
    f.write(raw)
print("appended, lines now:", raw.count("\n"))
