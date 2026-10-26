# -*- coding: utf-8 -*-
"""编剧 Agent 测试：标签归一 / 查库判据 / 备选记录归位 / 查库循环。

2026-10-11 加。背景：用户发现漫画 `画面:` 写成了自然语言
（`male hand grabbing her wrist from behind`）导致多手与错位，要求把编剧
从「单次 API 盲写」换成「Agent 查库写」。

这份测试钉死四件事：
  1. 标签归一（权重语法 / 转义括号 / artist 的 @ / 大小写）;
  2. **代码判据**：只判**语义**错（`hand around wrist` 语义错、`solo` 配第二方
     身体部位互斥、台词串进旁白栏），**不判存在性**——「库里查不到」只记日志、
     不判不合格（2026-10-11 用户定调：tags 库只是参考，AI 要有自我判断）；
  3. 备选行被抽走并挪到剧本头部，且解析后**不污染任何字段**
     （parse_script_text 会把未知行当续行拼进上一个字段，这是最易踩的坑）;
  4. agent 循环：工具结果回灌、不收敛时兜底交出最后一稿。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_agent


class NormTagTest(unittest.TestCase):
    """剧本里的标签文本 → 带空格的规范形态（**不转下划线**，见 _norm_tag 注释）。"""

    def test_plain_lower_and_spaces(self):
        self.assertEqual(comic_agent._norm_tag("School Uniform"), "school uniform")

    def test_weight_syntax(self):
        self.assertEqual(comic_agent._norm_tag("(chibi:2)"), "chibi")

    def test_escaped_parens(self):
        self.assertEqual(comic_agent._norm_tag("vertin \\(reverse:1999\\)"),
                         "vertin (reverse:1999)")

    def test_artist_at_prefix_stripped(self):
        self.assertEqual(comic_agent._norm_tag("@big chungus"), "big chungus")

    def test_series_with_colon_untouched(self):
        # 不带括号的系列名，冒号不是权重语法
        self.assertEqual(comic_agent._norm_tag("reverse:1999"), "reverse:1999")

    def test_db_key_underscores(self):
        self.assertEqual(comic_agent._db_key("hand around wrist"), "hand_around_wrist")


class MissingTagsTest(unittest.TestCase):
    """代码判据：逐个标签查库。"""

    def test_real_tags_pass(self):
        text = ("## 01\n画面: 1girl, solo, school uniform, hand around wrist, "
                "close-up\n气泡: 一\n")
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_fake_tag_caught(self):
        # arm_grab 看着很像对的，实测库里没有（真名是 hand_grab）
        text = "## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [("01", "arm_grab")])

    def test_prefix_tags_exempt(self):
        # 官方前缀标签不在 booru 词库里，规范第 2 节明确豁免
        text = ("## 01\n画面: masterpiece, best quality, safe, 1girl, solo\n"
                "气泡: 一\n")
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_year_and_score_exempt(self):
        text = "## 01\n画面: year 2025, score_7, 1girl\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_weight_wrapped_real_tag_passes(self):
        text = "## 01\n画面: 1girl, (close-up:1.5)\n气泡: 一\n"
        self.assertEqual(comic_agent.missing_tags(text), [])

    def test_reports_panel_number(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: 一\n\n"
                "## 02\n画面: 1girl, solo, medium shot\n气泡: 二\n")
        self.assertEqual(comic_agent.missing_tags(text), [("02", "medium_shot")])


class NoteExtractTest(unittest.TestCase):
    """备选记录必须抽走、挪到头部，且不能污染剧本字段。"""

    RAW = ("# 标题：公交站\n\n## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n"
           "备选: 选了 hand grab，另有 hand around wrist\n\n"
           "## 02\n画面: 1girl, solo\n气泡: 二\n")

    def test_extract_removes_note_lines(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        self.assertEqual(len(notes), 1)
        self.assertIn("hand around wrist", notes[0])
        self.assertNotIn("备选", clean)

    def test_place_notes_moves_to_header(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        out = comic_agent._place_notes(clean, notes)
        self.assertLess(out.index("[备选]"), out.index("## 01"))

    def test_placed_notes_do_not_pollute_fields(self):
        clean, notes = comic_agent._extract_notes(self.RAW)
        out = comic_agent._place_notes(clean, notes)
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 2)
        self.assertEqual(panels[0]["scene"], "1girl, solo, hand grab")
        self.assertEqual(panels[0]["bubble"], "一")
        self.assertEqual(panels[0]["label"], "")
        self.assertEqual(panels[1]["bubble"], "二")
        self.assertEqual(panels[1]["label"], "")

    def test_warning_line_keeps_own_prefix(self):
        out = comic_agent._place_notes("## 01\n画面: 1girl\n气泡: 一\n",
                                       ["[警告] 没过"])
        self.assertIn("[警告] 没过", out)
        # 不能叠成 `[备选] [警告] …`（实测踩过）
        self.assertNotIn("[备选] [警告]", out)

    def test_duplicate_notes_deduped(self):
        """同一格把同样的备选写两遍是常态，去重后只留一条。"""
        out = comic_agent._place_notes("## 01\n画面: 1girl\n",
                                       ["选了 A，另有 B", "选了 A，另有 B"])
        self.assertEqual(out.count("[备选]"), 1)

    def test_no_notes_is_passthrough(self):
        raw = "## 01\n画面: 1girl\n"
        self.assertEqual(comic_agent._place_notes(raw, []), raw)


class PreambleTest(unittest.TestCase):
    """剧本开头的碎碎念必须砍掉（模型交卷前的自言自语）。"""

    def test_strips_rambling_before_header(self):
        raw = ("剧本已足够：用 grabbing another's arm（抓他人手臂），"
               "配 from side 表现拉扯。\n\n"
               "# 标题：别闹了\n\n## 01\n画面: 1girl\n气泡: 一\n")
        out = comic_agent._strip_preamble(raw)
        self.assertTrue(out.startswith("# 标题：别闹了"))
        self.assertNotIn("剧本已足够", out)

    def test_keeps_text_that_starts_with_panel(self):
        raw = "## 01\n画面: 1girl"
        self.assertEqual(comic_agent._strip_preamble(raw), raw)

    def test_never_returns_empty_on_normal_script(self):
        raw = "# 标题：x\n\n## 01\n画面: 1girl\n气泡: 一\n"
        _, panels = comic.parse_script_text(comic_agent._strip_preamble(raw))
        self.assertEqual(len(panels), 1)


class CheckTest(unittest.TestCase):
    """契约校验 + 查库校验的合并判据。"""

    def test_accepts_good_script(self):
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo, pov, hand grab\n气泡: 一\n"
            "标签: 我攥住了她的书包带\n", 1)
        self.assertTrue(ok, why)

    def test_rejects_cjk_in_scene(self):
        ok, why = comic_agent.check("## 01\n画面: 1girl, 中文\n气泡: 一\n", 1)
        self.assertFalse(ok)
        self.assertIn("中文", why)

    def test_accepts_tag_missing_from_db(self):
        """「库里查不到」**不再**判不合格（2026-10-11 由用户定调）。

        tags 库只是参考，AI 要有自我判断。原来把它当硬回炉的后果是**惩罚写
        具体**：库里没有 `white_earphones` / `black_backpack`，模型老老实实写
        「白色耳机」就被打回去，只能退回泛化的 `earphones`——正好和规范
        「物件也要写颜色」正面撞车。现在只记日志，不进回炉消息。
        注意：`banned_tags` / `solo_conflicts` 仍是硬判据（那是**语义错**，
        不是「库里没有」），见下面两个测试类。
        """
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo, pov, white in-ear earphones\n气泡: 一\n"
            "标签: 她回过头\n", 1)
        self.assertTrue(ok, why)
        # 但函数本身还留着（日志用），并且照样能认出库外的词
        self.assertEqual(
            comic_agent.missing_tags(
                "## 01\n画面: 1girl, solo, white in-ear earphones\n气泡: 一\n"),
            [("01", "white_in-ear_earphones")])

    def test_rejects_wrong_panel_count(self):
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo\n气泡: 一\n", 2)
        self.assertFalse(ok)

    def test_rejects_missing_label(self):
        """`标签:` 从「可选」改成「每格必写」——用户报「剧情几乎没有」的修复。

        旧契约把它定义成可选的旁白条，模型就整行删掉，一话下来几乎没有剧情
        （只有气泡台词，没有旁白推进）。这条只能由代码兜：模型对「可选」的理解
        就是「能不写就不写」。
        """
        ok, why = comic_agent.check(
            "## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n", 1)
        self.assertFalse(ok)
        self.assertIn("标签", why)


class SoloConflictTest(unittest.TestCase):
    """`solo` 与「第二方身体部位入画」互斥。

    2026-10-11 用户三张实测图的根因：danbooru 官方角色计数规则里「一只手也算
    一个角色」，所以 `1girl, solo` 配「抓手腕」是自相矛盾的指令，模型只能
    三选一妥协（手变成她自己的 / 抓握整个消失 / 手畸形）。这些标签**库里都
    存在**，missing_tags 拦不住，只能查语义冲突。
    """

    @staticmethod
    def _script(scene, no="01"):
        return "## %s\n画面: %s\n气泡: 一\n标签: 我攥住了她的手腕\n" % (no, scene)

    def test_solo_plus_anothers_wrist(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding another's wrist"))
        self.assertEqual([("01", "holding_another's_wrist")], got)

    def test_solo_plus_anothers_wrist_without_apostrophe(self):
        """实测模型会漏撇号写成 `another wrist`，不能因此漏检。"""
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding another wrist"))
        self.assertEqual([("01", "holding_another_wrist")], got)

    def test_solo_plus_pov_hands_underscore(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, pov_hands"))
        self.assertEqual([("01", "pov_hands")], got)

    def test_solo_plus_disembodied_hand_reports_panel_no(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, disembodied hand", no="02"))
        self.assertEqual([("02", "disembodied_hand")], got)

    def test_holding_own_wrist_is_allowed(self):
        """抓自己手腕的格子，`solo` 是对的——不能误伤。"""
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, holding own wrist")))

    def test_solo_focus_with_other_is_allowed(self):
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo focus, 1other, holding another's wrist")))

    def test_plain_solo_is_allowed(self):
        self.assertEqual([], comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, standing, arm at side")))

    def test_check_rejects_solo_conflict(self):
        ok, why = comic_agent.check(
            self._script("safe, 1girl, solo, holding another's wrist"), 1)
        self.assertFalse(ok)
        self.assertIn("solo focus", why)


class BannedTagTest(unittest.TestCase):
    """「库里存在、语义却是错的」标签必须由代码专门拦。

    2026-10-11 用户实测：提示词里已明令把 `hand around wrist` 换成
    `holding another's wrist`，模型照样写它（库里另有 holding own wrist，
    正好对上它的语义），三张图 + 一稿剧本连续踩。存在性校验（missing_tags）
    对这类标签完全无效——只能专门盯。
    """

    def test_flags_hand_around_wrist(self):
        got = comic_agent.banned_tags(
            "## 01\n画面: safe, 1girl, solo, hand around wrist\n气泡: 一\n")
        self.assertEqual([("01", "hand_around_wrist")], got)

    def test_flags_underscore_form(self):
        """模型有时直接写下划线形态，两种都要认。"""
        got = comic_agent.banned_tags(
            "## 01\n画面: safe, 1girl, solo, hand_around_wrist\n气泡: 一\n")
        self.assertEqual([("01", "hand_around_wrist")], got)

    def test_correct_form_passes(self):
        got = comic_agent.banned_tags(
            "## 01\n画面: safe, 1girl, solo focus, 1other, "
            "holding another's wrist\n气泡: 一\n")
        self.assertEqual([], got)

    def test_check_points_at_the_replacement(self):
        ok, why = comic_agent.check(
            "## 01\n画面: safe, 1girl, solo, hand around wrist\n气泡: 一\n"
            "标签: 我抓住了她\n", 1)
        self.assertFalse(ok)
        self.assertIn("holding another's wrist", why)

    def test_check_reports_all_problems_at_once(self):
        """一次把问题全报出来——一类一条、改完再报下一条，两稿预算下改不完。

        2026-10-11 实跑逮到：同一稿里既有 `hand around wrist`（禁用标签）又有
        `solo` 配第二方身体部位（人数冲突），原来的写法命中第一条就 return，
        模型只被告知了后者，前者一路活到交稿。
        """
        text = ("## 01\n画面: safe, 1girl, solo, hand around wrist\n"
                "气泡: 一\n标签: 甲\n"
                "## 02\n画面: safe, 1girl, solo, holding another's wrist\n"
                "气泡: 一\n标签: 乙\n")
        ok, why = comic_agent.check(text, 2)
        self.assertFalse(ok)
        self.assertIn("holding another's wrist", why)    # 禁用标签那条
        self.assertIn("solo focus", why)                 # 人数冲突那条

    def test_check_catches_dialogue_written_as_a_label(self):
        """台词写进 `标签:` 栏必须被 check 拦下（它先走 `_validate`）。

        判据在 `_validate` 里，所以这里验证的是「agent 路径也确实吃到了」——
        实跑里正是 `标签2: （女）…` 被静默解析成旁白、气泡2 丢失。
        """
        text = ("## 01\n画面: safe, 1girl, solo\n气泡: （女）一\n"
                "标签1: 她一只脚踏上车门\n标签2: （女）我不要回去！\n")
        ok, why = comic_agent.check(text, 1)
        self.assertFalse(ok)
        self.assertIn("气泡2", why)


class WearableDetailTest(unittest.TestCase):
    """衣物 / 随身物件必须写明具体描述（颜色），泛类写法一律打回。

    2026-10-11 用户定调：「所有泛类必须明确的加上具体的描述，比如背包，衣领，
    裙子，衣服，之类的东西，都必须加明确的描述，是什么颜色的呀，什么形状的呀」。
    实测模型交出 `backpack, backpack on one shoulder`——它以为「带了姿态就算
    具体了」，而背包本身仍然没有颜色。
    """

    def _script(self, scene, no="01"):
        return "## %s\n画面: %s\n气泡: 一\n标签1: 她站在车门边\n" % (no, scene)

    def test_bare_backpack_is_flagged(self):
        got = comic_agent.bare_wearables(
            self._script("safe, 1girl, solo, backpack"))
        self.assertEqual([("01", "backpack")], got)

    def test_posture_is_not_a_color(self):
        """`backpack on one shoulder` 带了姿态，仍然没写颜色。"""
        got = comic_agent.bare_wearables(
            self._script("safe, 1girl, solo, backpack on one shoulder"))
        self.assertEqual([("01", "backpack_on_one_shoulder")], got)

    def test_style_is_not_a_color(self):
        got = comic_agent.bare_wearables(
            self._script("safe, 1girl, solo, sailor collar, pleated skirt"))
        self.assertEqual([("01", "sailor_collar"), ("01", "pleated_skirt")], got)

    def test_colored_wearable_passes(self):
        self.assertEqual([], comic_agent.bare_wearables(self._script(
            "safe, 1girl, solo, white uniform, white sailor collar, "
            "white pleated skirt, white socks, white in-ear earphones, "
            "white backpack on one shoulder")))

    def test_scene_props_are_not_policed(self):
        """场景物件（车门 / 扶手 / 车厢）不要求颜色——要求了只会误伤。"""
        self.assertEqual([], comic_agent.bare_wearables(self._script(
            "safe, 1girl, solo, standing, bus interior, automatic door, handrail, "
            "dim lighting, upper body")))

    def test_hand_tags_are_not_wearables(self):
        self.assertEqual([], comic_agent.bare_wearables(self._script(
            "safe, 1girl, solo focus, 1other, pov, pov hands, "
            "holding another's wrist, close-up")))

    def test_duplicate_wearable_reported_once(self):
        got = comic_agent.duplicate_wearables(self._script(
            "safe, 1girl, solo, white backpack, backpack on one shoulder"))
        self.assertEqual([("01", "backpack")], got)

    def test_duplicate_not_reported_across_categories(self):
        self.assertEqual([], comic_agent.duplicate_wearables(self._script(
            "safe, 1girl, solo, white shirt, white pleated skirt")))

    def test_check_rejects_bare_wearable(self):
        ok, why = comic_agent.check(
            self._script("safe, 1girl, solo, white uniform, white pleated skirt, "
                         "backpack, sailor collar"), 1)
        self.assertFalse(ok)
        self.assertIn("没写颜色", why)
        self.assertIn("backpack", why)

    def test_check_rejects_duplicate_wearable(self):
        ok, why = comic_agent.check(
            self._script("safe, 1girl, solo, white backpack, "
                         "backpack on one shoulder"), 1)
        self.assertFalse(ok)
        self.assertIn("两个包", why)

    def test_hyphenated_color_counts(self):
        """`black-framed glasses` 的颜色写在复合词里，不能判成「没写颜色」。

        2026-10-12 实跑误报：这一条把一份合格剧本白打回一稿（切词只按空格切，
        `black-framed` 整个词匹配不上色词表）。
        """
        self.assertEqual([], comic_agent.bare_wearables(self._script(
            "safe, 1girl, solo, white school uniform, black-framed glasses")))

    def test_bare_glasses_still_reported(self):
        """放宽连字符不能把判据废掉：真没写颜色的照旧报。"""
        got = comic_agent.bare_wearables(self._script(
            "safe, 1girl, solo, white school uniform, glasses"))
        self.assertEqual([("01", "glasses")], got)


class PovConflictTest(unittest.TestCase):
    """镜头人称定下就不许变：POV 剧本里绝不许出现 `1boy`。

    2026-10-11 用户原话：「如果是单人场景，不要写 1boy 这样的冲突的东西，我图三
    看到个男的很难受……明明前面 2 个图都是以手出现的，我这个角色的镜头视角抛出
    镜头，结果第三个场景就直接出问题」。
    """

    def _script(self, *scenes):
        return "".join(
            "## %02d\n画面: %s\n气泡: 一\n标签1: 她站在车门边\n" % (i + 1, s)
            for i, s in enumerate(scenes))

    def test_pov_plus_1boy_is_flagged(self):
        got = comic_agent.pov_conflicts(self._script(
            "safe, 1girl, solo, standing, bus interior",
            "safe, 1girl, solo focus, 1other, pov, pov hands, "
            "holding another's wrist",
            "safe, 1girl, 1boy, white uniform, black hoodie, grey pants, "
            "bus interior"))
        self.assertEqual([("03", "1boy")], got)

    def test_third_person_script_may_use_1boy(self):
        """2026-10-12 拍板后第三人称口子封死：`1boy` / `2girls` 无条件拦。"""
        got = comic_agent.pov_conflicts(self._script(
            "safe, 1girl, 1boy, white uniform, black hoodie, bus interior",
            "safe, 2girls, hug, classroom"))
        self.assertEqual([("01", "1boy"), ("02", "2girls")], got)

    def test_pov_missing_is_reported(self):
        """全篇默认 POV：没写视角标签的格要出清单（检测，不是闸门）。"""
        got = comic_agent.pov_missing(self._script(
            "safe, 1girl, solo, standing, bus interior",
            "safe, 1girl, solo, pov, standing, bus interior",
            "safe, 1girl, solo focus, 1other, pov, pov hands, "
            "holding another's wrist"))
        self.assertEqual([("01", "pov")], got)

    def test_pov_mixed_reports_minority_side(self):
        """`pov` 与 `female_pov` 不许一半一半：报少数派那侧（2026-10-12 实测）。"""
        got = comic_agent.pov_mixed(self._script(
            "safe, 1girl, solo, pov, standing",
            "safe, 1girl, solo, pov, walking",
            "safe, 1girl, solo focus, 1other, female_pov, pov hands, "
            "holding another's wrist",
            "safe, 1girl, solo focus, 1other, female_pov, pov hands, "
            "holding another's wrist"))
        self.assertEqual([("03", "female_pov"), ("04", "female_pov")], got)
        # 全篇统一时不报
        self.assertEqual([], comic_agent.pov_mixed(self._script(
            "safe, 1girl, solo, pov, standing",
            "safe, 1girl, solo, pov, walking")))

    def test_solo_plus_1boy_is_a_solo_conflict(self):
        got = comic_agent.solo_conflicts(
            self._script("safe, 1girl, solo, 1boy, standing"))
        self.assertEqual([("01", "1boy")], got)

    def test_check_points_at_1boy(self):
        ok, why = comic_agent.check(self._script(
            "safe, 1girl, solo focus, 1other, pov, pov hands, "
            "holding another's wrist",
            "safe, 1girl, 1boy, black hoodie, grey pants, bus interior"), 2)
        self.assertFalse(ok)
        self.assertIn("1boy", why)
        self.assertIn("pov", why)

    def test_pov_family_matches_space_form(self):
        """`_scene_tags` 归一是空格形态：写 `female pov`（空格）的格**不能**被
        pov_missing 误报（2026-10-12《小树林里的姐姐》实测误报）。"""
        script = self._script(
            "safe, 1girl, solo, standing, female pov",
            "safe, 1girl, solo focus, 1other, pov hands, female pov, "
            "holding another's wrist",
            "safe, 1girl, solo, standing")
        self.assertEqual([("03", "pov")], comic_agent.pov_missing(script))
        # pov_mixed 也一样：空格形态的 female pov 要被认出来
        mixed = self._script(
            "safe, 1girl, solo, standing, pov",
            "safe, 1girl, solo, standing, female pov")
        # 1:1 平票按少数派=female_pov 侧报
        self.assertEqual([("02", "female_pov")], comic_agent.pov_mixed(mixed))


class CompositionConflictTest(unittest.TestCase):
    """构图拍不到的部位不许写服饰标签。

    2026-10-12《小树林里的姐姐》02 格实测：构图 `upper body`（胸以上）却写了
    鞋类——画里的人没有脚，模型只能把鞋塞给镜头（观众），画面下角多出一双
    不属于任何人的小白鞋，整格崩坏。用户原话：「明明女生是上半身视角……
    小白鞋是谁的？小白鞋只能是我们这个视角的」。
    """

    def _script(self, *scenes):
        return "".join(
            "## %02d\n画面: %s\n气泡: 一\n标签1: 她站在车门边\n" % (i + 1, s)
            for i, s in enumerate(scenes))

    def test_upper_body_with_sneakers_is_flagged(self):
        got = comic_agent.composition_conflicts(self._script(
            "safe, 1girl, solo, white dress, upper body, female pov, "
            "white sneakers"))
        self.assertEqual([("01", "upper body", "white sneakers")], got)

    def test_close_up_flags_legwear_and_bottoms(self):
        got = comic_agent.composition_conflicts(self._script(
            "safe, 1girl, solo, black hair, close-up, navy pleated skirt, "
            "white over-kneehighs"))
        self.assertEqual(
            sorted([("01", "close-up", "navy pleated skirt"),
                    ("01", "close-up", "white over-kneehighs")]), sorted(got))

    def test_cowboy_shot_flags_shoes_but_not_kneehighs(self):
        """cowboy shot 拍到膝盖，over-kneehighs 部分可见，合法；鞋拍不到。"""
        got = comic_agent.composition_conflicts(self._script(
            "safe, 1girl, solo, cowboy shot, white over-kneehighs, "
            "black loafers"))
        self.assertEqual([("01", "cowboy shot", "black loafers")], got)

    def test_full_body_allows_footwear(self):
        self.assertEqual([], comic_agent.composition_conflicts(self._script(
            "safe, 1girl, solo, full body, white sneakers")))

    def test_no_composition_tag_no_check(self):
        self.assertEqual([], comic_agent.composition_conflicts(self._script(
            "safe, 1girl, solo, white sneakers")))

    def test_collect_issues_reports_composition_conflict(self):
        text = self._script(
            "safe, 1girl, solo, white dress, upper body, female pov, "
            "white sneakers")
        kinds = [i["kind"] for i in comic_agent.collect_issues(text, "需求")]
        self.assertIn("composition_conflict", kinds)

    def test_prompt_teaches_composition_scope(self):
        p = comic_agent._system_prompt(3)
        self.assertIn("构图决定可见范围", p)
        self.assertIn("构图拍到哪，服饰就写到哪", p)
        # 剧本规范全文也要带上这条
        self.assertIn("构图必须和服饰自洽", comic_agent._story_spec_text())


class PovDirectionTest(unittest.TestCase):
    """需求「视角:」行是视角方性别的唯一权威，标签侧对不上就报。

    2026-10-12《雨夜公交站》实测：需求明明是哥哥视角（哥哥和妹妹躲雨），
    编剧全篇自作主张写 `female pov`。`pov_mixed` 只能抓「一半一半」，
    抓不住「全篇统一地错」——用户拍板需求里手写「视角:」行当权威。
    """

    def _script(self, *scenes):
        return "".join(
            "## %02d\n画面: %s\n气泡: 一\n标签1: 她站在车门边\n" % (i + 1, s)
            for i, s in enumerate(scenes))

    def test_male_pov_line_flags_female_pov(self):
        brief = "视角: 哥哥\n主角: 妹妹\n剧情: 下雨天兄妹俩躲雨"
        got = comic_agent.pov_direction(brief, self._script(
            "safe, 1girl, solo, female pov, standing",
            "safe, 1girl, solo, pov, standing"))
        self.assertEqual([("01", "female pov")], got)

    def test_female_pov_line_flags_pov(self):
        brief = "视角: 姐姐\n主角: 妹妹\n剧情: 姐姐接妹妹放学"
        got = comic_agent.pov_direction(brief, self._script(
            "safe, 1girl, solo, pov, standing",
            "safe, 1girl, solo, female pov, standing"))
        self.assertEqual([("01", "pov")], got)

    def test_no_viewer_line_skips(self):
        brief = "下雨天兄妹俩躲雨，公交站一个人都没有"
        got = comic_agent.pov_direction(brief, self._script(
            "safe, 1girl, solo, female pov, standing"))
        self.assertEqual([], got)

    def test_ambiguous_viewer_word_skips(self):
        """「视角: 我」认不出性别 → 不猜，返回空。"""
        brief = "视角: 我\n主角: 妹妹\n剧情: 躲雨"
        got = comic_agent.pov_direction(brief, self._script(
            "safe, 1girl, solo, female pov, standing"))
        self.assertEqual([], got)

    def test_check_sends_direction_conflict_back(self):
        brief = "视角: 哥哥\n主角: 妹妹\n剧情: 躲雨"
        ok, why = comic_agent.check(self._script(
            "safe, 1girl, solo, female pov, standing, bus stop",
            "safe, 1girl, solo, female pov, standing, bus stop"), 2,
            brief)
        self.assertFalse(ok)
        self.assertIn("视角:", why)
        self.assertIn("哥哥", why)

    def test_prompt_teaches_pure_pov_and_bear_effect(self):
        """规则 5 白熊效应：pov 只是机位，「我」入画的唯一通道是 pov hands。"""
        p = comic_agent._system_prompt(3)
        self.assertIn("只标记机位", p)
        self.assertIn("唯一通道", p)
        self.assertIn("白熊效应", p)
        self.assertIn("需求的固定格式", p)
        self.assertIn("视角:", p)
        self.assertIn("主角:", p)


class MergedPeopleTest(unittest.TestCase):
    """多人同框必须写得出**两套**外貌，且点名谁是谁。

    2026-10-12 用户那一话：第 5 格 `2girls` 却把两套外貌平铺交叉（`navy pleated
    skirt` 写了两遍、中间夹着 `brown hair`），动作是分不出主宾的 `hug` —— 画出来
    谁抱谁、谁是学姐谁是学妹全乱。用户原话：「很明显的没有写清楚，girl1 是什么，
    girl2 是什么，girl1 拥抱 girl2，然后才是场景」。
    """

    def _script(self, *scenes):
        return "".join(
            "## %02d\n画面: %s\n气泡: （女）嗯\n标签1: 她站在教室里\n"
            % (i + 1, s) for i, s in enumerate(scenes))

    def test_two_girls_with_one_appearance_is_flagged(self):
        got = comic_agent.merged_people(self._script(
            "safe, 2girls, black hair, long hair, white sailor uniform, "
            "hug, classroom, cowboy shot"))
        self.assertEqual([("01", "2girls")], got)

    def test_two_girls_with_two_appearances_pass(self):
        """两个不同发色 = 两套外貌，能分清谁是谁。"""
        self.assertEqual([], comic_agent.merged_people(self._script(
            "safe, 2girls, black hair, long hair, white sailor uniform, "
            "brown hair, short hair, white coat, hug from behind, "
            "arm around another's waist, classroom, cowboy shot")))

    def test_english_phrase_counts_as_one_appearance_group(self):
        """一句英语短语里点了两个人的发色，也算两套（规范第 3 / 5 节的点名叫法）。"""
        self.assertEqual([], comic_agent.merged_people(self._script(
            "safe, 2girls, the brown-haired girl in a white coat is hugging "
            "the black-haired girl in a white sailor uniform from behind, "
            "hug from behind, classroom, cowboy shot")))

    def test_eye_color_does_not_count(self):
        """瞳色不算外貌组——`blue eyes` 人人都有，拿它计数会把判据糊过去。"""
        got = comic_agent.merged_people(self._script(
            "safe, 2girls, blue eyes, green eyes, hug, classroom, cowboy shot"))
        self.assertEqual([("01", "2girls")], got)

    def test_single_person_panel_untouched(self):
        self.assertEqual([], comic_agent.merged_people(self._script(
            "safe, 1girl, solo, black hair, white sailor uniform, classroom")))

    def test_check_rejects_merged_people(self):
        ok, why = comic_agent.check(self._script(
            "safe, 2girls, black hair, white sailor uniform, hug, classroom"), 1)
        self.assertFalse(ok)
        self.assertIn("只允许女主一个人", why)   # 新默认：多人数同框不再教写法，直接改单人格
        self.assertIn("pov hands", why)

    def test_prompt_teaches_female_pov_and_grouping(self):
        p = comic_agent._system_prompt(3)
        self.assertIn("female_pov", p)
        self.assertIn("男性第一人称", p)       # 为什么必须换：pov 的默认先验
        self.assertIn("画面里永远只有一个人物", p)   # 新规则 16：多人数同框退役
        self.assertIn("画面里永远只有女主一个人", p)  # 规则 6：人数标签只有两种写法
        # 主体那条：画面写的是女主的外貌，不是「我」
        self.assertIn("外貌永远是女主的", p)
        self.assertIn("整个消失", p)
        # 女女向视角那条：必须带「什么时候才写 pov」的边界，否则哥哥视角会被误伤
        self.assertIn("视角:」行是视角方性别的唯一权威", p)
        self.assertIn("female_pov, pov hands", p)      # 对例
        # `school uniform` 也是泛类，必须写颜色
        self.assertIn("white school uniform", p)

    def test_duplicate_wearable_skipped_for_two_girls(self):
        """两人同框时同名衣物**合法**：甲 `navy pleated skirt`、乙 `grey skirt`
        不该被判成「同一件写了两次」。

        2026-10-12 实跑误报：规则 16 的英语点名句里带着 `white cardigan` /
        `white sailor uniform`，加上两个女生的裙子/领子，`duplicate_wearables`
        一次报了 3 条（`sailor`/`cardigan`/`skirt`），把一份好剧本判成不合格、
        白烧一稿。
        """
        self.assertEqual([], comic_agent.duplicate_wearables(self._script(
            "safe, 2girls, the brown-haired girl in a white cardigan is hugging "
            "the black-haired girl in a white sailor uniform from behind, "
            "navy pleated skirt, white sailor collar, brown hair, "
            "white cardigan, grey skirt, hug from behind, classroom")))

    def test_duplicate_wearable_still_checked_for_solo_panel(self):
        """单人格照旧判——不能因为放宽多人格就把这条判据废掉。"""
        got = comic_agent.duplicate_wearables(self._script(
            "safe, 1girl, solo, white backpack, backpack on one shoulder"))
        self.assertEqual([("01", "backpack")], got)

    def test_duplicate_wearable_skipped_for_one_girl_one_boy(self):
        """`1girl, 1boy` 也是两人同框，点名句里的 `jacket` 不算写两遍。

        2026-10-12 实跑误报（`C20261012-05` 的 05/08 格）：一女一男没被认成多人，
        规范强制的英语点名句里那两件 `jacket` 被平铺计数，一口气误报 4 条。
        """
        self.assertEqual([], comic_agent.duplicate_wearables(self._script(
            "safe, 1girl, 1boy, the blonde-haired boy in a black jacket is "
            "washing the black-haired girl in a white serafuku, "
            "black jacket, white serafuku, blonde hair, black hair, bathroom")))


class FemalePovTest(unittest.TestCase):
    """女女向（百合）剧本的「我」是女性 → 第一人称视角必须写 `female_pov`。

    2026-10-12 用户实报：「剧本明确了是百合向，但是第一人称却是男生的手，
    这个是第一个问题」。根因是 `pov` 这个标签的先验就是**男性第一人称**，而
    规则 14 又不许「我」的人和服饰入画 —— 性别没有别的出口。
    """

    def _script(self, *scenes):
        return "".join(
            "## %02d\n画面: %s\n气泡: （女）嗯\n标签1: 她站在窗边\n"
            % (i + 1, s) for i, s in enumerate(scenes))

    _POV = ("safe, 1girl, solo focus, 1other, pov, pov hands, "
            "holding another's wrist")

    def test_pov_without_female_pov_is_flagged(self):
        got = comic_agent.female_pov_missing(
            self._script(self._POV), "性转，骨科转百合，青梅转百合")
        self.assertEqual([("01", "pov")], got)

    def test_female_pov_passes(self):
        self.assertEqual([], comic_agent.female_pov_missing(
            self._script("safe, 1girl, solo focus, 1other, female_pov, "
                         "pov hands, holding another's wrist"),
            "百合向"))

    def test_male_viewpoint_script_not_flagged(self):
        """哥哥视角那一话：「我」是男性，`pov` 本来就是对的，绝不能拦。"""
        self.assertEqual([], comic_agent.female_pov_missing(
            self._script(self._POV), "妹妹跟我赌气，拎着包就跑出了门"))

    def test_no_brief_skips_the_check(self):
        """不传需求就跳过（跟 invented_names 一样，可选判据）。"""
        self.assertEqual([], comic_agent.female_pov_missing(self._script(self._POV), ""))

    def test_check_rejects_pov_in_yuri_script(self):
        ok, why = comic_agent.check(
            self._script(self._POV), 1, brief="性转之后全校都乱套了，百合向")
        self.assertFalse(ok)
        self.assertIn("female_pov", why)


class RunRoundsTest(unittest.TestCase):
    """agent 循环：工具调用 → 结果回灌 → 收尾轮交出正文。"""

    def test_tool_round_then_script(self):
        replies = [
            '[[TOOL:search_tags]]{"query": "抓手"}',
            '## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            msgs = [{"role": "user", "content": "写个剧本"}]
            out = comic_agent.run_rounds(msgs, max_rounds=5)
        self.assertIn("画面", out)
        # 第二次调用时，工具结果必须已经回灌进 messages
        second_msgs = m.call_args_list[1].args[0]
        self.assertTrue(any("工具结果" in str(x.get("content") or "")
                            for x in second_msgs))

    def test_unknown_tool_is_reported_not_crashed(self):
        replies = [
            '[[TOOL:delete_file]]{"path": "x"}',
            '## 01\n画面: 1girl, solo\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        self.assertTrue(any("没有这个工具" in str(x.get("content") or "")
                            for x in m.call_args_list[1].args[0]))

    def test_llm_failure_returns_last_text(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=RuntimeError("boom")):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=3)
        self.assertEqual(out, "")

    def test_progress_callback_called(self):
        seen = []
        replies = [
            '[[TOOL:search_tags]]{"query": "眼泪"}',
            '## 01\n画面: 1girl, solo, tears\n气泡: 一\n',
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies):
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5, progress=seen.append)
        self.assertTrue(any("search_tags" in s for s in seen))

    def test_first_round_without_tools_is_nudged(self):
        """第一轮直接交正文必须被打回——实测提示词挡不住，模型觉得自己知道。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            script,                                    # 第 1 轮：不查库直接交卷
            '[[TOOL:search_tags]]{"query": "眼泪"}',     # 被打回后去查
            script,                                    # 收尾
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        second = m.call_args_list[1].args[0]
        self.assertTrue(any("还没有查过库" in str(x.get("content") or "")
                            for x in second))

    def test_nudge_happens_only_once(self):
        """打回只来一次：模型仍不查就认了，不能无限打回烧 token。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=script) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        self.assertEqual(m.call_count, 2)

    # ─── 轮次预算：查库段用完必须强制动笔 ─────────────────
    #
    # 2026-10-11 用户实测：两稿 24 轮全烧在 search_tags 上，一次正文都没交，
    # 下游报「只解析到 0 格」。

    def test_forced_write_round_closes_tools(self):
        """查库段用完要往对话里插「查库时间到」，逼它动笔。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "一"}',   # 1 查库
            '[[TOOL:search_tags]]{"query": "二"}',   # 2 查库（最后一个查库轮）
            script,                                  # 3 强制轮：正文
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)
        self.assertEqual(m.call_count, 3)
        third = m.call_args_list[2].args[0]
        self.assertTrue(any("查库时间到" in str(x.get("content") or "")
                            for x in third))

    def test_forced_round_does_not_execute_tools(self):
        """强制轮里模型再输出工具调用也**不能执行**——一执行就把这一轮又变回

        查库轮，永远收敛不了。
        """
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "一"}',   # 1 查库
            '[[TOOL:search_tags]]{"query": "二"}',   # 2 查库
            '[[TOOL:search_tags]]{"query": "三"}',   # 3 强制轮仍查库 → 丢弃
            script,                                  # 4 强制轮：正文
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies), \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）") as t:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)
        self.assertEqual(t.call_count, 2)        # 只有前两轮真的执行了工具

    def test_exhausted_rounds_returns_process_draft(self):
        """到顶也没交出正文时，中间轮的半截稿要兜住，不能返回空串。

        原版必然返回空串——唯一给兜底变量赋值的就是「无工具调用」那个分支，
        而那个分支本身就是 return，所以模块文档承诺的兜底从没生效过。
        """
        half = ('## 01\n画面: 1girl, solo\n气泡: 一\n'
                '[[TOOL:search_tags]]{"query": "眼泪"}')
        only_tool = '[[TOOL:search_tags]]{"query": "眼泪"}'
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[half, only_tool, only_tool, only_tool]):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)

    def test_nudged_body_is_kept_as_fallback(self):
        """第 1 轮被打回的那份稿子要留作兜底。

        模型可能被打了回之后就一直查库、再也不交稿——那时宁可交一份「标签没
        验过」的稿子（下游还有出口校验和人工确认），也不要交空白。
        """
        first = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        only_tool = '[[TOOL:search_tags]]{"query": "眼泪"}'
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[first, only_tool, only_tool, only_tool]):
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=4)
        self.assertIn("画面", out)

    def test_tool_result_carries_round_budget(self):
        """回灌工具结果时要报剩余轮数——预算得让模型自己能算。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = ['[[TOOL:search_tags]]{"query": "眼泪"}', script]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m:
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        second = m.call_args_list[1].args[0]
        fed = " ".join(str(x.get("content") or "") for x in second)
        self.assertIn("还剩", fed)
        self.assertIn("强制", fed)

    # ─── 跨轮去重 + 结果上限（2026-10-11 成本复盘）─────────────────
    #
    # 实测 22:05 那次运行：223 次 search_tags 里 **76 次是跨轮重复**（轮内 0 次），
    # `pov hands` / `backlighting` 各查 5 遍；8 次运行每次都查满 10 轮才被动笔。
    # 成因是 MAX_TOOL_CHARS=2000 把每轮索要的 2,232~4,346 字符砍掉 10%~53%。

    def test_repeated_query_is_not_refetched(self):
        """同一个词跨轮重查不再执行工具，只回一句「已查过」。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "眼泪"}',   # 1 查
            '[[TOOL:search_tags]]{"query": "眼泪"}',   # 2 重查 → 不执行
            script,
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies), \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）") as t:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         max_rounds=5)
        self.assertIn("画面", out)
        self.assertEqual(t.call_count, 1)

    def test_repeat_notice_is_told_to_the_model(self):
        """去重时要把「已查过」告诉模型，否则它以为没查到、下轮接着查。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "眼泪"}',
            '[[TOOL:search_tags]]{"query": "眼泪"}',
            script,
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies) as m, \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）"):
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        # 第 3 次调用时，那条「已查过」的说明必须已经回灌进 messages
        third = m.call_args_list[2].args[0]
        fed = " ".join(str(x.get("content") or "") for x in third)
        self.assertIn("已经查过", fed)
        self.assertEqual(fed.count("（假结果）"), 1)      # 只贴过一次原文

    def test_query_dedup_ignores_case_and_space(self):
        """`Tears` 和 ` tears ` 是同一个词，不该各查一遍。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        replies = [
            '[[TOOL:search_tags]]{"query": "Tears"}',
            '[[TOOL:search_tags]]{"query": "  tears  "}',
            script,
        ]
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=replies), \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）") as t:
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        self.assertEqual(t.call_count, 1)

    def test_random_search_is_never_deduped(self):
        """`random` 抽标签刻意不去重——同样的参数本来就该给出不同结果。"""
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        one = '[[TOOL:search_tags]]{"random": true, "cat": "pose"}'
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[one, one, script]), \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="（假结果）") as t:
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        self.assertEqual(t.call_count, 2)

    def test_tool_results_survive_the_old_2000_cap(self):
        """一轮 9 个各 500 字符的结果（4,500 字符）要完整回灌。

        旧上限 2000 会把后面 5 个查询的结果整个砍掉——模型看不到就重查。
        这条同时守住「不许把上限调回 2000」。
        """
        script = "## 01\n画面: 1girl, solo\n气泡: 一\n"
        nine = "\n".join('[[TOOL:search_tags]]{"query": "q%d"}' % i
                         for i in range(9))
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[nine, script]) as m, \
                mock.patch.object(comic_agent, "_run_tool",
                                  return_value="x" * 500):
            comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                   max_rounds=5)
        second = m.call_args_list[1].args[0]
        fed = " ".join(str(x.get("content") or "") for x in second)
        self.assertNotIn("截断", fed)
        self.assertGreater(len(fed), 4000)
        self.assertEqual(fed.count("x" * 500), 9)


class WriteScriptTest(unittest.TestCase):
    """对外入口：合格即返回（含头部备选），不合格回炉、最后一次容忍。"""

    def test_returns_script_with_note_header(self):
        good = ("# 标题：公交站\n\n## 01\n画面: 1girl, solo, hand grab\n"
                "气泡: 一\n备选: 选了 hand grab，另有 hand around wrist\n")
        with mock.patch.object(comic_agent, "run_rounds", return_value=good):
            out = comic_agent.write_script("公交站被抓包", panels=1)
        self.assertIn("[备选]", out)
        self.assertLess(out.index("[备选]"), out.index("## 01"))
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["scene"], "1girl, solo, hand grab")

    def test_retry_then_pass(self):
        bad = ("# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n")
        good = ("# 标题：x\n\n## 01\n画面: 1girl, solo, hand grab\n气泡: 一\n")
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=[bad, good]) as m:
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertEqual(m.call_count, 2)
        self.assertIn("hand grab", out)
        # 回炉那一轮必须带上错因，且**不带**上一轮的工具中间结果
        second_msgs = m.call_args_list[1].args[0]
        self.assertTrue(any("不合格" in str(x.get("content") or "")
                            for x in second_msgs))

    def test_last_try_tolerated_with_warning(self):
        bad = ("# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n")
        with mock.patch.object(comic_agent, "run_rounds", return_value=bad):
            out = comic_agent.write_script("x", panels=1, tries=1)
        self.assertIn("[警告]", out)
        # 警告必须在头部，不能落到格内污染台词
        _, panels = comic.parse_script_text(out)
        self.assertEqual(panels[0]["bubble"], "一")

    def test_empty_draft_retries_with_targeted_prompt(self):
        """空正文不能和「有稿但标签不对」走同一条回炉路。

        空正文回炉若带一条**空的** assistant 消息，模型看不出自己错在哪（它
        眼里那就是"上一份稿子"），于是接着查库。这一路要用专门的话点明「你没
        交剧本」。
        """
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=["", good]) as m:
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertIn("画面", out)
        second = m.call_args_list[1].args[0]
        self.assertTrue(any("没有交剧本" in str(x.get("content") or "")
                            for x in second))
        self.assertFalse(any(x.get("role") == "assistant" and not x.get("content")
                             for x in second))

    def test_all_empty_raises(self):
        """一稿都没写出正文就报错——返回空串会让调用方落一个空 script.md。"""
        with mock.patch.object(comic_agent, "run_rounds", return_value=""):
            with self.assertRaises(RuntimeError):
                comic_agent.write_script("x", panels=1, tries=2)

    def test_empty_then_keeps_earlier_draft(self):
        """后一稿空、前一稿有内容：最后一稿不能被空正文覆盖掉。"""
        bad = "# 标题：x\n\n## 01\n画面: 1girl, solo, arm grab\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               side_effect=[bad, ""]):
            out = comic_agent.write_script("x", panels=1, tries=2)
        self.assertIn("[警告]", out)
        _, panels = comic.parse_script_text(out)
        self.assertEqual(len(panels), 1)
        self.assertEqual(panels[0]["bubble"], "一")

    def test_empty_brief_raises(self):
        with self.assertRaises(ValueError):
            comic_agent.write_script("   ")

    def test_provider_defaults_to_current_not_chain_head(self):
        """留空要落到「全局生效的那家」，不能让 call_llm 从降级链头开跑。

        2026-10-11 实跑逮到：provider 传 None 时日志是 `volc / deepseek-v4-flash`
        —— llm.call_llm 在 provider 为空时直接走降级链，`.env` 的
        LLM_PROVIDER=deepseek 被绕过。
        """
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=good) as m, \
                mock.patch.object(comic_agent.llm, "CURRENT_PROVIDER",
                                  "deepseek"):
            comic_agent.write_script("x", panels=1)
        self.assertEqual(m.call_args.kwargs.get("provider"), "deepseek")

    def test_explicit_provider_wins(self):
        good = "# 标题：x\n\n## 01\n画面: 1girl, solo\n气泡: 一\n"
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=good) as m:
            comic_agent.write_script("x", panels=1, provider="volc",
                                     model="m1")
        self.assertEqual(m.call_args.kwargs.get("provider"), "volc")
        self.assertEqual(m.call_args.kwargs.get("model"), "m1")


class PromptTest(unittest.TestCase):
    """system prompt 必须带规范全文与真实标签示例。"""

    def test_contains_full_spec_and_no_placeholder(self):
        p = comic_agent._system_prompt(3)
        self.assertIn("规范全文开始", p)
        self.assertIn("Anima 标签规范", p)
        self.assertNotIn("__SPEC__", p)
        self.assertNotIn("__PANELS__", p)
        self.assertNotIn("__MAX_BUBBLE__", p)

    def test_lists_real_hand_tags_and_fake_ones(self):
        p = comic_agent._system_prompt(3)
        # 真名（实测在库）：抓别人手腕的正解 + 第一人称手 + 无身体的手
        for real in ("holding another's wrist", "pov hands", "disembodied hand"):
            self.assertIn(real, p)
        # 假名（实测不在库）：必须出现在黑名单里
        for fake in ("sfw", "arm grab", "male hand", "medium shot", "soft lighting"):
            self.assertIn(fake, p)

    def test_teaches_solo_focus_rule(self):
        """人数规则必须写进提示词：出现第二方身体部位时不许写 `solo`。

        2026-10-11：`1girl, solo` 配「抓手腕」是自相矛盾的指令（danbooru
        官方计数规则里一只手也算一个角色），模型只能三选一妥协。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("solo focus", p)
        self.assertIn("1other", p)
        # `hand around wrist` 只能出现在「反例 · 别用」的语境里
        self.assertIn("hand around wrist", p)
        self.assertIn("她自己抓自己", p)

    def test_teaches_recorder_camera_rule(self):
        """规则 19：画面是「我」的眼睛看到的，她的位置由旁白决定。

        2026-10-12《妈妈的味道》01 格实测：旁白「我推开门，厨房传来炒菜声」，
        画面却写 `standing, doorway, genkan` 把她画在玄关——把「我」的位置套到
        她身上。规则必须写成错/对示例，还得点明「她不在视野的瞬间不成格」。
        同日晚二犯：旁白只说「传来炒菜声」（没写她移动），画面又把她画回
        `open door, genkan` 迎人 → 补「她不挪窝」默认律 + 逐格三问自查。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("记录者", p)
        self.assertIn("她的位置跟着旁白走", p)
        self.assertIn("她不会替「我」推门", p)
        self.assertIn("不单独成格", p)
        # 她不挪窝：移动必须是旁白明写的节拍；打招呼 ≠ 站到面前
        self.assertIn("她不挪窝", p)
        self.assertIn("开口打招呼 ≠ 她已经站到我面前", p)
        # 二犯的错/对标签对必须原文进规则
        self.assertIn("open door, genkan, looking at viewer, surprised", p)
        self.assertIn("kitchen, stove, cooking, looking back, surprised", p)
        # 逐格三问自查
        self.assertIn("逐格三问", p)
        self.assertIn("改标签，不是改旁白", p)
        # 旁白写「我」、画面写「她」：我的状态绝不生成画面标签（含沙发反例）
        self.assertIn("旁白写「我」，画面写「她」", p)
        self.assertIn("连沙发都不出现", p)
        self.assertIn("sitting, couch, living room, looking at viewer", p)
        self.assertIn("standing, genkan, removing_shoes, looking back", p)
        # 规则 20：互动动作的双手账——她对我只写「朝镜头」，落点在我身上的不写
        self.assertIn("互动动作的双手账", p)
        self.assertIn("reaching_towards_viewer", p)
        self.assertIn("双手独占", p)
        self.assertIn("画面里没有我的身体，模型看不见落点", p)
        self.assertIn("我的手抵住她胸口", p)

    def test_rule20_viewer_family_ban_and_landing_test(self):
        """规则 20 收口（2026-10-12 晚拍板）：落点测试前置 + `*_viewer` 动作族
        按族禁（含库外自造短语），安全区写清——没有落点的朝镜头写法 +
        `viewer_holding_*`（我的手拿东西）。

        《妈妈的小憩》03 格实测：写「她的手抵住我胸口」→ 方向画反成
        「我的手抵住她胸口」，且 `arms around viewer's waist` 还是库外自造。
        """
        p = comic_agent._system_prompt(3)
        # 落点测试前置：三分类问句
        self.assertIn("先过落点测试", p)
        self.assertIn("落点在她身上", p)
        self.assertIn("朝镜头、没有落点", p)
        self.assertIn("落点在我身上", p)
        # 安全区：没有落点的朝镜头写法 + viewer_holding_*
        for safe in ("looking_at_viewer", "reaching_towards_viewer",
                     "running_towards_viewer", "walking_towards_viewer",
                     "facing_viewer", "viewer_holding_leash",
                     "viewer_holding_phone"):
            self.assertIn(safe, p)
        # 按族禁：接触类整族 *_viewer 动作标签 + 库外自造短语
        self.assertIn("只有「接触类」整族禁写", p)
        self.assertIn("`pointing_at_viewer`（指向我）", p)
        self.assertIn("指向 / 招手 / 递东西不是这类", p)
        for bad in ("grabbing_viewer", "hugging_viewer", "kabedon_on_viewer",
                    "leading_viewer", "headpatting_viewer", "feeding_viewer",
                    "kissing_viewer", "arms around viewer's waist"):
            self.assertIn(bad, p)
        # 规则 14 不许再推荐 grabbing / hugging viewer（与规则 20 自相矛盾）
        self.assertNotIn("（抓我们的手）", p)
        self.assertNotIn("这族标签库里齐全", p)
        self.assertIn("边界与原因见规则 20", p)

    def test_rule20_direction_by_subject_not_landing(self):
        """规则 20 补丁（2026-10-18）：方向靠「主语 + 手在不在场」定，不靠词义猜。

        《学姐带我逛校园》03 格实测：旁白「她手指朝我一指」被写成
        `pov hands` + `pointing_at_another` + `looking back`，生出来是
        「我的手戳她」——`another` 与 `pov hands` 争同一只手，`looking back`
        又让她的指向不可能成立。三条对应的收口必须写进提示词。
        """
        p = comic_agent._system_prompt(3)
        # ① pointing_at_viewer 进安全区，且明确不属于接触类
        self.assertIn("pointing_at_viewer", p)
        self.assertIn("招手用 `beckoning`", p)
        self.assertIn("别自造", p)
        # ② another 族只能由「我」当主语
        self.assertIn("`another` 族只能由「我」当主语", p)
        self.assertIn("这动作是我做的吗", p)
        # ③ 她动手的格子我的手不入场
        self.assertIn("她动手的格子，我的手不许在场", p)
        self.assertIn("这一格的动作是「我」主动做的吗", p)
        # ④ 背身格不写朝镜头动作
        self.assertIn("背身格不许再写「朝镜头」的动作", p)
        self.assertIn("保住背身、把动作整个丢掉", p)
        # 补正 / 讨论两路自动同步（切块取自同一出处）
        self.assertIn("背身格不许再写", comic_agent._fix_system_prompt())
        self.assertIn("背身格不许再写", comic_agent._chat_system_prompt())

    def test_fix_prompt_audits_appearance(self):
        """补正员要审外观（2026-10-18 用户强调）。

        现场：`hairclip` 被补成 `orange hairclip` —— 只补了颜色，形状和位置
        仍缺，模型每格会把发饰换个地方戴。脸和身上的东西是观众认人的锚，
        补正必须按「颜色 + 形状 + 位置」补全，不能补一半。
        """
        f = comic_agent._fix_system_prompt()
        self.assertIn("外观审计和视角审计同等重要", f)
        self.assertIn("颜色 + 形状 + 位置", f)
        self.assertIn("orange bow hairclip on left side of head", f)
        self.assertIn("不写位置，模型每格会把它换个地方戴", f)
        self.assertIn("这种「补了一半」也算漏", f)
        # 服饰那一类也在（颜色 + 款式，blue skirt → blue pleated skirt）
        self.assertIn("blue skirt", f)
        self.assertIn("blue pleated skirt", f)

    def test_narrator_lock_viewer_is_camera_body(self):
        """旁白锁 A（2026-10-12 晚拍板）：`标签:` 里的「我」＝观众＝镜头本体，
        不是女主的自称；她的内心走气泡（（女）前缀）+ 表情标签。"""
        p = comic_agent._system_prompt(3)
        self.assertIn("旁白锁", p)
        self.assertIn("永远指观众", p)
        self.assertIn("不是女主的自称", p)
        self.assertIn("（女）", p)
        # 补正员的头号职责就是审这一堆（清单没列到的也管）
        f = comic_agent._fix_system_prompt()
        self.assertIn("头号职责是审计视角", f)
        self.assertIn("落点测试", f)
        self.assertIn("数量形态", f)
        self.assertIn("旁白锁", f)
        self.assertIn("1other", f)
        self.assertIn("清单没列到的同样要管", f)

    def test_panel_count_injected(self):
        self.assertIn("正好 7 格", comic_agent._system_prompt(7))

    def test_fix_and_chat_prompts_carry_pov_rules(self):
        """补正员 / 修订助手也必须带 POV 视角硬规则（2026-10-12 用户拍板：
        「补写 ai 也得知道这些东西」）。规则从编剧提示词里切，单一出处。"""
        for prompt in (comic_agent._fix_system_prompt(),
                       comic_agent._chat_system_prompt()):
            self.assertIn("互动动作的双手账", prompt)
            self.assertIn("记录者", prompt)
            self.assertIn("改构图，绝不删服饰", prompt)
            self.assertIn("画面里没有我的身体，模型看不见落点", prompt)
            # 规则 20 收口后的按族禁 + 落点测试，两个改稿 AI 都得带
            self.assertIn("先过落点测试", prompt)
            self.assertIn("只有「接触类」整族禁写", prompt)
            self.assertIn("pointing_at_viewer", prompt)
            self.assertIn("viewer_holding", prompt)
            # 旁白锁 A 同步进改稿 AI
            self.assertIn("旁白锁", prompt)
            # 数量形态两种写法也要进改稿 AI（04 格悬空 1other 就是修订助手
            # 只看到规则 14–20、没看到通用区规则 6 造成的）
            self.assertIn("人数标签只有两种写法", prompt)
            self.assertIn("补一整个人进来", prompt)
            # 标记本身要被切掉，不能残留在给模型的提示里
            self.assertNotIn("开始】", prompt)
            self.assertNotIn("结束】", prompt)

    def test_tool_call_format_is_taught(self):
        """必须写明 [[TOOL:...]]{...} 格式。

        2026-10-11 实跑踩到：提示词里只说了「先查库」没说**怎么调**，模型
        输出了 29 行 `search_tags: 抓手腕` 这样的纯文本（不是工具调用），
        parse_tool_calls 一个都没解析到，整轮白烧。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn('[[TOOL:search_tags]]{"query"', p)
        self.assertIn("[[TOOL:read_file]]", p)
        self.assertIn("[[TOOL:web_search]]", p)
        self.assertIn("查库只能写成上面的", p)

    def test_teaches_round_budget(self):
        """必须把轮次预算告诉它：查库段用完会强制动笔（工具关闭）。

        2026-10-11 用户实测：两稿 24 轮全烧在 search_tags 上，一次正文都没交。
        提示词只写「先查完再写」没用——它不知道自己在烧最后的机会。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("轮次预算", p)
        self.assertIn("不要查第二遍", p)
        self.assertIn("15~25", p)             # 要教它一轮批量查，别一轮 5 个
        # 强制动笔那条指令本身
        self.assertIn("查库时间到", comic_agent._FORCE_WRITE)
        self.assertIn("工具已经关闭", comic_agent._FORCE_WRITE)

    def test_fake_alias_marked_missing(self):
        """`grabbing another's wrist` / `wrist grab` 实测**不在**本地库里。

        上一版提示词把它们当可用别名推荐（写错了），模型照着查会落空。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("在本库里**都不存在**", p)

    def test_teaches_multi_label_narration(self):
        """旁白从「一条可选的状态读数」改成「每格必写的多条剧情旁白」。

        2026-10-11 用户报「剧情几乎没有」。旧定义是「只用于交代时间/前提跳转」，
        模型就只写「傍晚 · 末班车到站」这类读数，或者干脆整行删掉；一话下来
        除了台词没有任何叙事推进。同一天追加：一格要能讲好几段剧情
        （`标签1:` `标签2:` `标签3:`）。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("标签1:", p)
        self.assertIn("标签2:", p)
        self.assertIn("每一格至少要有一条", p)
        self.assertIn("第一人称", p)
        self.assertIn("不要写成时间地点", p)

    def test_story_spec_is_injected(self):
        """《连续剧情提示词生成规范》必须真的喂进去。

        2026-10-11 用户报「感觉 AI 没遵守那份 skill 的规范」——查出来是根本
        没喂：那份规范只有旧 CLI（comic_writer）在读，网页端的编剧换成
        comic_agent 时漏了注入。实跑核对过 `'连续剧情' in prompt` 曾是 False。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("连续剧情提示词生成规范", p)
        self.assertIn("连续剧情规范全文开始", p)
        self.assertIn("不取整句写法", p)         # 规范是自然语言示例，只取规则 + 空间短语
        self.assertNotIn("__STORY_SPEC__", p)
        self.assertNotIn("__MAX_LABELS__", p)

    def test_teaches_story_line_rule(self):
        """整话必须是一条故事线，后一格不许推翻前一格已经建立的事实。

        用户贴的一话：01「别跟着我……」说明她已经知道有人跟着，02 又「谁——！」
        变成刚发现，自相矛盾；三格还都是同一个瞬间的三个切面。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("整话必须是一条故事线", p)
        self.assertIn("不是同一个瞬间的三个切面", p)
        self.assertIn("别跟着我", p)
        self.assertIn("谁——！", p)

    def test_teaches_explicit_speaker(self):
        """每条气泡都要带说话人——它决定气泡尾巴指向谁。

        用户原话：「给 qwen 的指令必须明确，箭头指向谁必须是确定的」。写不清
        qwen 就把镜头外「我」说的话挂到女生头上（见 comic._tail_note）。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("每条气泡都必须带说话人", p)
        self.assertIn("（女）", p)
        self.assertIn("（我）", p)

    def test_teaches_panel_progression(self):
        """每格必须有新信息，不许两格只是换表情演同一个动作。

        用户实测稿：02/03 都是「攥着手腕」，只是一个 surprised、一个 angry，
        信息增量接近零——观众看到的是同一格。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("每格必须带来新信息", p)
        self.assertIn("交稿前自己回看一遍", p)

    def test_hand_is_not_required_every_panel(self):
        """视角方的手按剧情需要入画，不是每格都必须出现。

        用户 2026-10-11 原话：「并不是每个图片都要出现我们的手」。硬塞手部
        标签会把那一格推进「solo 配一只手」的矛盾里（见规则 6）。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("不要每格都加一只手", p)


    def test_teaches_spec_over_tag_db(self):
        """**规范 > 标签库**：库里 `pleated_skirt` / `earphones` / `backpack`
        这类"款式 / 物件名"本身不带颜色，照抄就违反规范的「颜色+款式必须写全」。

        2026-10-11 用户截图实测：`pleated skirt` 没写颜色，三格画面里裙子一次
        深藏青、一次浅蓝。用户定调：「anima-tags 只是一个参考，我的核心规范是
        那份 skills，他才是一切的核心」。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("高于 anima-tags 标签库", p)
        self.assertIn("两者冲突时一律以规范", p)
        self.assertIn("pleated skirt", p)
        self.assertIn("navy pleated skirt", p)
        self.assertIn("white in-ear earphones", p)
        self.assertIn("每一格都要写同一个颜色", p)
        # 规范文件本身也要有这条
        self.assertIn("物件同样禁止泛化", p)
        self.assertIn("本规范 > 标签库", p)

    def test_panel_is_the_only_image_prompt(self):
        """只有 `画面:` 进生图提示词；`标签:`/`气泡:` 是画到图上的文字。

        用户原话：「标签的意思是让观众知道发生了什么，气泡是展示当下的场景是
        什么」——三者有关联，但**不是**「标签直接变成生图提示词」。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("只有 `画面:` 是生图提示词", p)
        self.assertIn("都不进生图提示词", p)
        self.assertIn("生图阶段**完全不知道**", p)
        self.assertIn("静止可见的样子", p)

    def test_no_longer_recommends_standing_on_one_leg(self):
        """坏推荐必须彻底删掉。

        2026-10-11 实跑翻车：规则 8 黑名单里原本写着
        「`stepping off` / `getting off`（用 `standing on one leg` 之类）」
        ——我在教模型拿单脚站立顶替"下车"。结果旁白「她一只脚刚踏下车门」被
        逐字翻成 `standing on one leg`，画成芭蕾姿。
        （`standing on one leg` 现在只作为**反例**出现，所以断言的是那句推荐语
        本身不在了，不是这个词不出现。）
        """
        p = comic_agent._system_prompt(3)
        # 旧推荐语是**括号里的替换建议**：`stepping off` / `getting off`（用 … 之类）
        self.assertNotIn("（用 standing on one leg 之类）", p)
        self.assertNotIn("（用 `standing on one leg` 之类）", p)
        self.assertIn("单脚站立的芭蕾姿", p)      # 反例还在，且讲清了为什么
        self.assertIn("瞬间动作", p)

    def test_teaches_single_location_per_panel(self):
        """一格只能有一个地点，同场景跨格保持一致。

        2026-10-11 用户截图：三格都同时写了 `bus interior` 和
        `bus stop / outdoors / city`，同一个公交场景三格各不相同。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("一格只能有一个明确地点", p)
        self.assertIn("同一个地点", p)
        self.assertIn("这一场景内保持不动", p)

    def test_no_longer_claims_pure_tag_writing(self):
        """写法从「纯标签」放开成「标签为主 + 介词短语补空间关系」。

        用户原话：「我们的组合是短词和英语短语来着」——旧提示词却写着
        「本工作流用**纯标签**写法，规范第 5 节的混合写法本流程不用」，
        和用户要的正好相反。
        2026-10-26 再放开一档：方位 / 距离这类标签库里根本没有的词
        （实测 `on table` / `to the left` 查不到），必须允许英文介词短语。
        """
        p = comic_agent._system_prompt(3)
        self.assertNotIn("本工作流用**纯标签**写法", p)
        self.assertIn("标签为主 + 允许介词短语补位置", p)
        self.assertIn("介词短语", p)
        self.assertIn("不含主谓", p)              # 「什么算短语」的边界写清楚了
        self.assertIn("on the left side of the red wooden desk", p)
        self.assertIn("不是白名单", p)
        self.assertIn("三·补 A", p)               # 状态阶梯
        self.assertIn("三·补 B", p)               # 空间锚点
        # 开头那句最显眼的话也不再是「每个标签都必须真实存在于库里」
        self.assertNotIn("每一个标签都必须", p)
        self.assertIn("唯一的生图提示词", p)

    def test_teaches_wearable_must_have_color(self):
        """泛类必须加具体描述：**款式 / 姿态都替代不了颜色**。

        2026-10-11 用户原话：「所有泛类必须明确的加上具体的描述，比如背包，衣领，
        裙子，衣服，之类的东西，都必须加明确的描述，是什么颜色的呀，什么形状的
        呀」。实测模型写 `backpack, backpack on one shoulder` 就算交差了。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("写了款式 / 姿态", p)
        self.assertIn("不等于", p)
        self.assertIn("white sailor collar", p)
        self.assertIn("white backpack on one shoulder", p)
        self.assertIn("两个包", p)
        # 规范文件也要有这两条
        self.assertIn("同一件东西只写一个标签", p)

    def test_teaches_pov_consistency(self):
        """全篇默认第一人称 POV：画面里只有女主，`1boy` / `2girls` 任何格都不许写。

        2026-10-12 用户拍板原话：「我们默认就是以第一人称 POV 视角……画面里面
        的主角就一个就行了，就一个女主……如果说什么逃跑中遇到黄毛，其实就
        相当于我们就是黄毛，并不是说黄毛出现在画面里面」。
        """
        p = comic_agent._system_prompt(3)
        self.assertIn("全篇默认第一人称 POV", p)
        self.assertIn("任何情况下都不许写 `1boy` / `2girls`", p)
        self.assertIn("观众本人扮演", p)
        self.assertIn("不许给「我」写", p)
        self.assertIn("looking at viewer", p)     # 她对镜头做动作的标签族
        # 旧提示词那句「那只手明显是男性手臂时可写 1boy 代替 1other」必须彻底删掉
        self.assertNotIn("可写 `1boy` 代替", p)
        self.assertNotIn("是男性就写 `1boy`", p)


def _script(body_01, body_02=None, body_03=None):
    """拼一份最小可解析的剧本（默认三格，只给 01 也能跑）。"""
    out = ["# 标题：测试"]
    for no, body in (("01", body_01), ("02", body_02), ("03", body_03)):
        if body is None:
            continue
        out.append("## %s\n%s\n气泡: （女）嗯。\n标签1: 她在做什么。" % (no, body))
    return "\n".join(out)


class BareHairColorTest(unittest.TestCase):
    """发色判据：画了头发就必须写发色。

    由来（2026-10-11 十轮）：用户那份「逃不掉的放学路」三格全写 `twintails,
    blue eyes` 却**一个发色都没写**，而规范第二节明写「发色必须写死」。
    编剧 AI 漏了，补正 AI 也没补上（flash 版），只有代码能兜住。
    """

    def test_reports_when_hairstyle_without_color(self):
        text = _script(
            "画面: safe, 1girl, solo, twintails, blue eyes, white sailor collar",
            "画面: safe, 1girl, solo, twintails, blue eyes, white sailor collar")
        got = comic_agent.bare_hair_color(text)
        self.assertEqual([no for no, _ in got], ["01", "02"])

    def test_eye_color_does_not_count_as_hair_color(self):
        """`blue eyes` 是瞳色，不是发色——不能把这条判据糊过去。

        这是最容易写错的一处：只要判据写成「整格出现色词就算」，`blue eyes`
        会让三格全部通过，判据等于没用。
        """
        text = _script("画面: safe, 1girl, solo, twintails, blue eyes")
        self.assertEqual(len(comic_agent.bare_hair_color(text)), 1)

    def test_accepts_black_hair(self):
        text = _script("画面: safe, 1girl, solo, black hair, twintails, blue eyes")
        self.assertEqual(comic_agent.bare_hair_color(text), [])

    def test_accepts_color_and_style_in_one_tag(self):
        """`blonde twintails` 也是写了发色（色词和发型在同一个标签里）。"""
        text = _script("画面: safe, 1girl, solo, blonde twintails, blue eyes")
        self.assertEqual(comic_agent.bare_hair_color(text), [])

    def test_accepts_multiword_hair_color(self):
        for tag in ("silver-white long hair", "long silver hair", "black short hair"):
            with self.subTest(tag=tag):
                self.assertEqual(
                    comic_agent.bare_hair_color(_script("画面: safe, 1girl, %s" % tag)),
                    [])

    def test_silent_when_no_hair_mentioned(self):
        """一格完全没提头发就不报——背影、剪影、只画手的 POV 格都会误伤。"""
        text = _script("画面: safe, 1girl, pov, pov hands, dark")
        self.assertEqual(comic_agent.bare_hair_color(text), [])

    def test_mentions_in_check_message(self):
        text = _script("画面: safe, 1girl, solo, twintails, blue eyes")
        ok, why = comic_agent.check(text, 1)
        self.assertFalse(ok)
        self.assertIn("发色", why)


class InventedNamesTest(unittest.TestCase):
    """凭空人名判据：画面里的角色名只能来自原始需求。

    由来（2026-10-11 十轮）：补正 AI（deepseek-v4-pro）在补发色的同时塞进了
    `Izumi Sagiri`——那是规范第 74 行示例里的角色名，被当素材抄了。后果是画面
    里会画出一个完全不相干的角色，而且很难第一眼发现。
    """

    def test_reports_name_not_in_brief(self):
        text = _script("画面: safe, 1girl, Izumi Sagiri, black hair, twintails")
        got = comic_agent.invented_names(text, "妹妹离家出走，被哥哥抓包")
        self.assertEqual(got, [("01", "Izumi Sagiri")])

    def test_allows_name_from_brief(self):
        text = _script("画面: safe, 1girl, Izumi Sagiri, black hair, twintails")
        self.assertEqual(comic_agent.invented_names(text, "画一张 Izumi Sagiri"), [])

    def test_allows_name_from_original_draft(self):
        text = _script("画面: safe, 1girl, Izumi Sagiri, black hair")
        allowed = "需求里没这个名字\n但原稿里有 Izumi Sagiri"
        self.assertEqual(comic_agent.invented_names(text, allowed), [])

    def test_ignores_normal_multiword_tags(self):
        """普通多词标签（含下划线式）不能误报——所以判据只认 Title Case 人名。"""
        text = _script("画面: safe, 1girl, white backpack on one shoulder, "
                       "black hair, holding another's wrist, backlighting")
        self.assertEqual(comic_agent.invented_names(text, "需求"), [])

    def test_check_only_guards_when_brief_given(self):
        """不传 brief 就跳过这条判据——老调用点（check(text, n)）行为不变。"""
        text = _script("画面: safe, 1girl, pov, Izumi Sagiri, black hair")
        self.assertTrue(comic_agent.check(text, 1)[0])
        self.assertFalse(comic_agent.check(text, 1, "需求里没这个名字")[0])


class DiffScriptsTest(unittest.TestCase):

    def test_only_reports_changed_panels_and_fields(self):
        a = _script("画面: safe, 1girl, twintails", "画面: safe, 1girl, solo")
        b = _script("画面: safe, 1girl, black hair, twintails",
                    "画面: safe, 1girl, solo")
        diff = comic_agent.diff_scripts(a, b)
        self.assertEqual(len(diff), 1)
        self.assertEqual(diff[0]["no"], "01")
        self.assertEqual(diff[0]["changed"], ["scene"])
        self.assertIn("black hair", diff[0]["after"]["scene"])
        self.assertNotIn("black hair", diff[0]["before"]["scene"])

    def test_identical_scripts_give_empty_diff(self):
        a = _script("画面: safe, 1girl, black hair")
        self.assertEqual(comic_agent.diff_scripts(a, a), [])


class CollectIssuesTest(unittest.TestCase):

    def test_structured_issue_list(self):
        text = _script("画面: safe, 1girl, twintails, backpack, Izumi Sagiri")
        issues = comic_agent.collect_issues(text, "需求里没有那个名字")
        kinds = {it["kind"] for it in issues}
        self.assertIn("missing_hair_color", kinds)
        self.assertIn("bare_wearable", kinds)
        self.assertIn("invented_name", kinds)
        for it in issues:
            self.assertTrue(it["no"] and it["detail"])

    def test_clean_script_gives_no_issues(self):
        text = _script("画面: safe, 1girl, solo, pov, black hair, twintails, "
                       "white backpack on one shoulder")
        self.assertEqual(comic_agent.collect_issues(text, "需求"), [])


class FixScriptTest(unittest.TestCase):
    """补正一遍：代码出清单（**只做检测**）→ 模型照清单补 → **直接采纳**。

    ⚠️ 2026-10-12 用户拍板取消准入闸门：补正稿一律采纳，代码判据只出清单 +
    出体检报告。唯一不采纳的是「模型没交作业」（空 / 解析不出剧本 / 格号对不上）。
    """

    BRIEF = "妹妹离家出走，被哥哥抓包，black hair, twintails"
    DRAFT = _script("画面: safe, 1girl, solo, twintails, blue eyes")
    FIXED = _script("画面: safe, 1girl, solo, black hair, twintails, blue eyes")

    def test_empty_checklist_still_runs_full_review(self):
        """清单为空也照样调模型——代码只看标签层，语义问题（视角悖论、旁白
        与画面矛盾、剧情倒退）它永远看不见。「清单空就跳过」＝代码替模型
        决定「不用看」，2026-10-12 晚随准入闸门一起拆掉。"""
        clean = _script("画面: safe, 1girl, solo, pov, black hair, twintails, "
                        "white backpack on one shoulder")
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=clean) as m:
            r = comic_agent.fix_script(self.BRIEF, clean)
        self.assertTrue(m.called)
        self.assertFalse(r["skipped"])
        # 清单为空时提示词必须改口成「全文自审」
        sent = m.call_args[0][0][1]["content"]
        self.assertIn("全文", sent)
        self.assertIn("代码判据没拦到", sent)

    def test_adopts_verified_fix(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=self.FIXED):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertTrue(r["adopted"])
        self.assertEqual(r["text"], self.FIXED)
        self.assertEqual([d["no"] for d in r["diff"]], ["01"])
        self.assertIn("black hair", r["text"])

    def test_adopts_invented_name_but_flags_it(self):
        """补正稿凭空多出人名 → **照样采纳**，只在体检报告 audit 里标出来。

        2026-10-12 用户拍板：代码判据只做检测、不做准入闸门。宁可留着一条可疑的
        名字让用户自己看（界面会显示「补正后仍报 N 处」），也不许把整份补正丢掉。
        """
        bad = _script("画面: safe, 1girl, solo, Izumi Sagiri, black hair, twintails")
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=bad):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertTrue(r["adopted"])
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["text"], bad)
        self.assertTrue(any(it["kind"] == "invented_name" for it in r["audit"]))

    def test_adopts_change_to_unflagged_panel(self):
        """动了清单没点名的格 → 也采纳（只是逐格差异会照实列出来）。"""
        draft = _script("画面: safe, 1girl, solo, twintails, blue eyes",
                        "画面: safe, 1girl, solo, black hair, twintails, "
                        "white backpack on one shoulder")
        bad = _script("画面: safe, 1girl, solo, black hair, twintails, blue eyes",
                      "画面: safe, 1girl, solo, black hair, twintails, "
                      "white backpack on one shoulder, warm light")
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=bad):
            r = comic_agent.fix_script(self.BRIEF, draft)
        self.assertTrue(r["adopted"])
        self.assertEqual(sorted(d["no"] for d in r["diff"]), ["01", "02"])

    def test_adopts_even_if_issues_remain(self):
        """补正员只补了一部分：剩下的那些**不拦**，改对多少保住多少。"""
        half = _script("画面: safe, 1girl, solo, black hair, twintails, backpack")
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=half):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertTrue(r["adopted"])
        self.assertTrue(any(it["kind"] == "bare_wearable" for it in r["audit"]))

    def test_unusable_when_panel_count_changes(self):
        """格号对不上＝改结构，不是补描述 → 不采纳，提示用户重跑。"""
        bad = _script("画面: safe, 1girl, solo, black hair, twintails, blue eyes",
                      "画面: safe, 1girl, solo, black hair, twintails")
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=bad):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertFalse(r["adopted"])
        self.assertEqual(r["status"], "structure")
        self.assertEqual(r["text"], self.DRAFT)
        self.assertTrue(any("重新补正" in x for x in r["warnings"]))

    def test_unusable_when_not_a_script(self):
        """返回的东西没有 `## NN` 骨架 → 解析不出剧本 → 不采纳，提示重跑。"""
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value="好的，我已经把颜色补上了。"):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertFalse(r["adopted"])
        self.assertEqual(r["status"], "unparseable")
        self.assertEqual(r["text"], self.DRAFT)

    def test_unusable_when_empty_output(self):
        with mock.patch.object(comic_agent.llm, "call_llm", return_value="  "):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertFalse(r["adopted"])
        self.assertEqual(r["status"], "empty")
        self.assertEqual(r["text"], self.DRAFT)
        self.assertTrue(any("重新补正" in x for x in r["warnings"]))

    def test_call_failure_returns_draft(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=RuntimeError("boom")):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertFalse(r["adopted"])
        self.assertEqual(r["status"], "error")
        self.assertEqual(r["text"], self.DRAFT)
        self.assertTrue(any("重新补正" in x for x in r["warnings"]))

    def test_no_change_is_not_adopted(self):
        """模型把原稿原样交回：没改动就不算采纳（但也不算错）。"""
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=self.DRAFT):
            r = comic_agent.fix_script(self.BRIEF, self.DRAFT)
        self.assertFalse(r["adopted"])
        self.assertEqual(r["status"], "nochange")
        self.assertEqual(r["diff"], [])

    def test_issue_list_is_fed_to_model(self):
        """清单必须真的喂进去——不给清单它就自由发挥（实测编出 `warm light`）。"""
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=self.FIXED) as m:
            comic_agent.fix_script(self.BRIEF, self.DRAFT)
        fed = " ".join(str(x.get("content") or "")
                       for x in m.call_args.args[0])
        self.assertIn("问题清单", fed)
        self.assertIn("missing_hair_color", fed)
        self.assertIn("发色", fed)

    def test_fix_prompt_forbids_copying_spec_examples(self):
        """系统提示必须点明「规范里的示例角色名不是素材」——`Izumi Sagiri` 就是这么进来的。"""
        p = comic_agent._fix_system_prompt()
        self.assertIn("示例角色名", p)
        self.assertIn("不是素材", p)
        self.assertIn("原始需求", p)


class ChatReviseTest(unittest.TestCase):
    """剧本对话修订（2026-10-12）：聊天框里一句话，要么交回改好的剧本，
    要么回一段「答复：」。采纳判据与补正同一条哲学——**只判格式，不判内容**，
    交回了能解析、格号对得上的剧本就采纳（撤销按钮负责兜底）。"""

    SCRIPT = _script("画面: safe, 1girl, solo, pov, twintails, blue eyes")
    CHANGED = _script("画面: safe, 1girl, solo, pov, long straight hair, blue eyes")

    def _call(self, ret, **kw):
        with mock.patch.object(comic_agent.llm, "call_llm", return_value=ret) as m:
            r = comic_agent.chat_revise(kw.pop("script", self.SCRIPT),
                                        kw.pop("message", "女主加长发"),
                                        **kw)
        return r, m

    def test_reply_form_strips_marker(self):
        r, _ = self._call("答复：02 格用 pov 是因为镜头就是你的眼睛。")
        self.assertFalse(r["changed"])
        self.assertIsNone(r["script"])
        self.assertIn("pov", r["reply"])
        self.assertNotIn("答复：", r["reply"])

    def test_reply_form_accepts_bold_marker(self):
        r, _ = self._call("**答复：**你想改哪一格？")
        self.assertFalse(r["changed"])
        self.assertIn("改哪一格", r["reply"])

    def test_adopts_changed_script(self):
        r, _ = self._call(self.CHANGED)
        self.assertTrue(r["changed"])
        self.assertEqual(r["script"], self.CHANGED)
        self.assertEqual([d["no"] for d in r["diff"]], ["01"])
        self.assertIn("格 01", r["reply"])

    def test_identical_script_is_not_a_change(self):
        r, _ = self._call(self.SCRIPT)
        self.assertFalse(r["changed"])
        self.assertIsNone(r["script"])
        self.assertTrue(r["warnings"])

    def test_unparseable_output_shown_not_landed(self):
        r, _ = self._call("已把女主改成长发啦！")
        self.assertFalse(r["changed"])
        self.assertIn("已把女主改成长发啦！", r["reply"])
        self.assertTrue(any("解析不成剧本" in x or "找不到任何一格" in x
                            for x in r["warnings"]))

    def test_structure_mismatch_not_landed(self):
        bad = _script(self.CHANGED.split("## 01\n", 1)[1].split("\n气泡", 1)[0],
                      "画面: safe, 1girl, solo, pov, black hair")
        r, _ = self._call(bad)
        self.assertFalse(r["changed"])
        self.assertTrue(any("格号" in x for x in r["warnings"]))

    def test_empty_output_asks_retry(self):
        r, _ = self._call("  ")
        self.assertFalse(r["changed"])
        self.assertIn("空", r["reply"])

    def test_call_failure_returns_message(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=RuntimeError("boom")):
            r = comic_agent.chat_revise(self.SCRIPT, "女主加长发")
        self.assertFalse(r["changed"])
        self.assertIn("调用失败", r["reply"])

    def test_history_and_brief_are_fed_in(self):
        hist = [{"role": "user", "content": "上句话"},
                {"role": "assistant", "content": "上句回答"},
                {"role": "system", "content": "这种要被丢掉"}]
        r, m = self._call("答复：好", history=hist, brief="哥哥视角的躲雨故事")
        self.assertFalse(r["changed"])
        fed = " ".join(str(x.get("content") or "") for x in m.call_args.args[0])
        self.assertIn("上句话", fed)
        self.assertIn("哥哥视角的躲雨故事", fed)
        self.assertNotIn("这种要被丢掉", fed)

    def test_empty_message_rejected(self):
        with self.assertRaises(ValueError):
            comic_agent.chat_revise(self.SCRIPT, "   ")

    def test_chat_prompt_defines_two_forms(self):
        p = comic_agent._chat_system_prompt()
        self.assertIn("答复：", p)
        self.assertIn("完整剧本", p)
        self.assertIn("分镜规范全文", p)
        self.assertIn("不加格", p)   # 2026-10-26：明确「加格删格不是你的活」

    def test_panel_count_fact_is_fed_in(self):
        """2026-10-26：把「当前剧本共几格」当事实塞进 user 消息。

        由来：作者 17 格的稿子，模型自己多加一格 `## 18` 交回来，格号一比就
        整份被拒，作者只看到「AI 返回完整剧本」。抽象规则（格号不许增删）
        不如把具体数字摆在眼前。
        """
        three = _script("画面: safe, 1girl, solo",
                        "画面: safe, 1girl, solo",
                        "画面: safe, 1girl, solo")
        r, m = self._call("答复：好", script=three)
        fed = " ".join(str(x.get("content") or "") for x in m.call_args.args[0])
        self.assertIn("当前剧本共 3 格", fed)
        self.assertIn("## 01 … ## 03", fed)
        self.assertIn("正好 3 个", fed)

    def test_extra_panel_gets_human_reply_not_full_script(self):
        """多交一格时不把 8000 字原稿糊进聊天框，只报事实。"""
        two = _script("画面: safe, 1girl, solo",
                      "画面: safe, 1girl, solo")
        extra = two + "\n## 03\n画面: safe, 1girl, solo, sad\n气泡: （女）嗯。\n"
        r, _ = self._call(extra, script=two)
        self.assertFalse(r["changed"])
        self.assertIsNone(r["script"])
        self.assertIn("多出 ## 03", r["reply"])
        self.assertIn("交回 3 格，当前剧本是 2 格", r["reply"])
        self.assertNotIn("画面:", r["reply"])        # 不再整份贴出来
        self.assertTrue(any("格号" in x for x in r["warnings"]))

    def test_prose_reply_without_marker_is_shown_as_is(self):
        """模型忘写 `答复：` 直接说人话 —— 正文里没有 `## NN`，按讨论展示。"""
        r, _ = self._call("你想改哪一格？先说清楚我再动。")
        self.assertEqual(r["reply"], "你想改哪一格？先说清楚我再动。")
        self.assertFalse(r["changed"])


class PrevStoryTest(unittest.TestCase):
    """前情提要（2026-10-18 连续剧需求）。

    新一集编剧时得知道前面演到哪了，否则每集从零开始、认过的面反复认。
    抽取只留「气泡 + 旁白」：画面行是发色/服饰/景别/灯光，给生图用的，
    带进前情既费字数，还会误导编剧把上一集的衣服照抄过来。
    """

    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_keeps_bubbles_and_narration_drops_scene(self):
        p = self._write(
            "# 标题：t\n\n## 01\n画面: 1girl, solo, navy skirt, close-up\n"
            "气泡: （女）你好\n标签1: 她抬起头。\n标签2: 窗外在下雨。\n\n"
            "## 02\n画面: 1girl, solo\n气泡2: （我）嗯\n标签1: 我应了一声。\n")
        out = comic_agent.prev_story_text(p)
        self.assertIn("## 01", out)
        self.assertIn("（女）你好", out)
        self.assertIn("她抬起头。", out)       # 多条旁白都在
        self.assertIn("窗外在下雨。", out)
        self.assertIn("（我）嗯", out)          # 气泡2 的说话人
        self.assertNotIn("navy skirt", out)    # 画面行整段丢掉
        self.assertNotIn("画面:", out)

    def test_truncates_to_limit(self):
        body = "\n\n".join("## %02d\n（女）第%d句\n旁白%d" % (i, i, i)
                           for i in range(1, 60))
        out = comic_agent.prev_story_text(self._write(body), limit=100)
        self.assertLessEqual(len(out), 101)
        self.assertTrue(out.endswith("…"))

    def test_missing_file_is_empty(self):
        self.assertEqual("", comic_agent.prev_story_text("这个文件不存在.md"))

    def test_user_prompt_carries_continuity_rules(self):
        from app import comic_writer
        p = comic_writer._user_prompt("设定", 3, "", "## 01\n（女）上集台词")
        self.assertIn("前情：", p)
        self.assertIn("上集台词", p)
        self.assertIn("不许重演前情里的情节", p)
        self.assertIn("人物关系、情绪和已经发生的事必须延续", p)
        # 没有前情时一个字都不许出现（全新一集不该被这段话干扰）
        self.assertNotIn("前情：", comic_writer._user_prompt("设定", 3, "", ""))


class SkipSearchTest(unittest.TestCase):
    """跳过查库（2026-10-22 用户要的「默认跳过检索 AI」）。

    钉死三件事：
      1. 提示词真的换了——跳过版里不许再有「第一步永远是查库」和工具调用示例，
         否则模型会先吐一轮 `[[TOOL:...]]` 白烧一轮（那时工具是关的）；
      2. 循环真的不执行工具——`_run_tool` 一次都不许被调；
      3. 轮数收敛到 NO_SEARCH_ROUNDS，不会用满 12 轮在原地打转。
    """

    SCRIPT = "# 标题：公交站\n\n## 01\n画面: 1girl, solo, black hair\n气泡: 一\n"

    def test_prompt_swaps_tools_section(self):
        normal = comic_agent._system_prompt(3)
        skip = comic_agent._system_prompt(3, no_search=True)
        self.assertIn("第一步永远是查库", normal)
        self.assertIn("[[TOOL:search_tags]]", normal)
        self.assertNotIn("第一步永远是查库", skip)
        self.assertNotIn("[[TOOL:search_tags]]", skip)
        self.assertIn("没有工具", skip)
        # 规则一条都不许少：规范正文和 POV 硬规则两版共用
        for kw in ("Danbooru", "POV", "画面:", "气泡:"):
            self.assertIn(kw, skip)
        self.assertGreater(len(skip), len(normal) * 0.9)

    def test_first_round_writes_without_any_tool(self):
        with mock.patch.object(comic_agent.llm, "call_llm",
                               return_value=self.SCRIPT) as m, \
                mock.patch.object(comic_agent, "_run_tool") as t:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         no_search=True)
        self.assertIn("1girl", out)
        self.assertEqual(m.call_count, 1)      # 一轮就交稿
        t.assert_not_called()                  # 一个工具都没执行

    def test_tool_calls_are_not_executed_and_nudged_once(self):
        """模型刹不住还吐工具调用时：不执行、只催稿，催完照收正文。"""
        calls = '\n'.join('[[TOOL:search_tags]]{"query": "q%d"}' % i
                          for i in range(3))
        with mock.patch.object(comic_agent.llm, "call_llm",
                               side_effect=[calls, self.SCRIPT]) as m, \
                mock.patch.object(comic_agent, "_run_tool") as t:
            out = comic_agent.run_rounds([{"role": "user", "content": "x"}],
                                         no_search=True)
        self.assertIn("1girl", out)
        self.assertEqual(m.call_count, 2)
        t.assert_not_called()
        fed = " ".join(str(x.get("content") or "")
                       for x in m.call_args_list[1].args[0])
        self.assertIn("工具已经关闭", fed)
        # 跳过查库时不能说「用你已经查到的」
        self.assertNotIn("用你已经查到的", fed)

    def test_write_script_caps_rounds(self):
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=self.SCRIPT) as m:
            comic_agent.write_script("x", panels=1, no_search=True)
        self.assertTrue(m.call_args.kwargs.get("no_search"))
        self.assertLessEqual(m.call_args.kwargs.get("max_rounds"),
                             comic_agent.NO_SEARCH_ROUNDS)

    def test_write_script_passes_flag_to_prompt(self):
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=self.SCRIPT) as m:
            comic_agent.write_script("x", panels=1, no_search=True)
        system = m.call_args.args[0][0]["content"]
        self.assertIn("没有工具", system)


class FreeViewModeTest(unittest.TestCase):
    """自由视角模式（2026-10-22 用户要的第二个版本）。

    背景：POV 只是漫画的一种展示形式，双人同框同样是漫画；规范本身（
    `comic_spec.md`）就是「单人与男女双人特化版」，是 2026-10-12 加的第十一节
    把双人封死的。所以要一条「回到规范、机位自己挑」的路。

    钉死四件事：
      1. system 真的换版：没有 POV 硬规则（规则 14–20），有自由视角规则，
         规范里第十一节与总结第 7 条**被裁掉**（口头声明压不住矛盾规则）；
      2. `check`：`1boy` / 双人同框 / 没写视角标签在 free 下全放过，pov 下照拦；
      3. 通用判据一条都不许跟着松（禁用标签、没写颜色、凭空人名）；
      4. 补正员与修订助手也换版——否则它们会拿着 POV 规则把双人改回单人。
    """

    SINGLE = ("# 标题：x\n\n## 01\n画面: 1girl, solo, pov, black hair, "
              "white shirt\n气泡: 一\n标签1: 她站在门口\n")
    DOUBLE = ("# 标题：x\n\n## 01\n画面: 1girl, 1boy, black hair, white shirt, "
              "short black hair, black hoodie, standing, bedroom\n"
              "气泡: 一\n标签1: 他站在门口\n")

    def test_system_prompt_swaps_pov_rules(self):
        pov = comic_agent._system_prompt(3)
        free = comic_agent._system_prompt(3, pov_mode="free")
        self.assertIn("POV 视角硬规则（规则 14–20）开始", pov)
        self.assertNotIn("POV 视角硬规则", free)
        self.assertIn("自由视角规则（规则 14–17）开始", free)
        # 规则 1–13（标签写法、颜色、地点、构图）两版共用，一条都不许少
        for kw in ("标签为主 + 允许介词短语补位置", "navy pleated skirt",
                   "一格只能有一个明确地点"):
            self.assertIn(kw, free)
        # 模板正文里那两处以「全篇第一人称」为前提的声明也必须换掉
        self.assertIn("但这一话不强制", free)
        self.assertIn("人数标签按画面里真实出现的人写", free)
        self.assertNotIn("人数标签只有两种写法", free)

    def test_story_spec_drops_pov_chapters(self):
        """规范里的「唯一 POV」两处必须被代码切掉，不能靠一句「本次不作数」。"""
        full = comic_agent._story_spec_text()
        cut = comic_agent._story_spec_text("free")
        self.assertIn("十一、镜头人称", full)
        self.assertIn("全篇第一人称 POV**", full)
        self.assertNotIn("十一、镜头人称", cut)
        self.assertNotIn("全篇第一人称 POV**", cut)
        # 第十节、第十二节的其它内容还在（只切 POV 那两处）
        self.assertIn("十、画面只写", cut)
        self.assertIn("十二、本质总结", cut)
        self.assertLess(len(cut), len(full))

    def test_check_allows_two_people_only_in_free(self):
        ok_pov, why = comic_agent.check(self.DOUBLE, 1, pov_mode="pov")
        self.assertFalse(ok_pov)
        self.assertIn("1boy", why)
        ok_free, why_free = comic_agent.check(self.DOUBLE, 1, pov_mode="free")
        self.assertTrue(ok_free, why_free)
        # 单人 POV 稿两种模式都合格（自由模式不禁止 POV，只是不强制）
        self.assertTrue(comic_agent.check(self.SINGLE, 1)[0])
        self.assertTrue(comic_agent.check(self.SINGLE, 1, pov_mode="free")[0])

    def test_free_still_blocks_generic_issues(self):
        """自由视角是放宽机位，不是放宽画面质量——这几条通用判据一条都不能松。"""
        banned = ("# 标题：x\n\n## 01\n画面: 1girl, solo, hand around wrist, "
                  "black hair\n气泡: 一\n标签1: 甲\n")
        self.assertFalse(comic_agent.check(banned, 1, pov_mode="free")[0])
        bare = ("# 标题：x\n\n## 01\n画面: 1girl, 1boy, black hair, pleated skirt, "
                "short black hair, black hoodie, room\n气泡: 一\n标签1: 甲\n")
        ok, why = comic_agent.check(bare, 1, pov_mode="free")
        self.assertFalse(ok)
        self.assertIn("pleated skirt", why)
        # 凭空人名照拦（需求里没有的名字）
        named = ("# 标题：x\n\n## 01\n画面: 1girl, 1boy, Izumi Sagiri, black hair, "
                 "white shirt, black hair, black hoodie, room\n气泡: 一\n标签1: 甲\n")
        self.assertIn("专有人名", comic_agent.check(named, 1, "哥哥和妹妹",
                                                  pov_mode="free")[1])

    def test_collect_issues_free_drops_pov_kinds(self):
        pov_issues = {i["kind"] for i in comic_agent.collect_issues(self.DOUBLE)}
        free_issues = {i["kind"] for i in
                       comic_agent.collect_issues(self.DOUBLE, pov_mode="free")}
        self.assertTrue({"pov_conflict", "pov_missing"} & pov_issues)
        self.assertFalse({"pov_conflict", "pov_missing", "merged_people",
                          "solo_conflict", "female_pov", "pov_mixed",
                          "pov_direction"} & free_issues)

    def test_fix_and_chat_prompt_follow_mode(self):
        for fn in (comic_agent._fix_system_prompt, comic_agent._chat_system_prompt):
            self.assertIn("POV 视角硬规则", fn("pov"))
            free = fn("free")
            self.assertNotIn("POV 视角硬规则", free)
            self.assertIn("自由视角规则", free)
            # 规范里那两处也得跟着裁，否则补正员照样读到「唯一 POV」
            self.assertNotIn("十一、镜头人称", free)

    def test_write_script_forwards_mode(self):
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=self.SINGLE) as m:
            comic_agent.write_script("x", panels=1, pov_mode="free")
        system = m.call_args.args[0][0]["content"]
        self.assertIn("自由视角规则", system)

    def test_write_script_checks_with_mode(self):
        """回炉判据也要带上模式：不带的话双人稿在 free 下永远不合格、反复回炉。"""
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=self.DOUBLE), \
                mock.patch.object(comic_agent, "check",
                                  return_value=(True, "")) as chk:
            comic_agent.write_script("x", panels=1, pov_mode="free")
        self.assertEqual(chk.call_args.kwargs.get("pov_mode"), "free")


class ThirdViewModeTest(unittest.TestCase):
    """第三人称档（2026-10-22 用户要的第三档）。

    用户原话：「就算我选择自由视角，ai其实是倾向于pov视角的。」——free 给了
    选择权，但 POV 措辞散在模板正文 5 处（规则 5 手部、规则 6 人数、规则 14–20、
    旁白示例、旁白人称作答），模型读到的仍然是一部 POV 剧本。third 档把这 5 处
    全部换掉，并明令禁止 POV 系标签。

    钉死五件事：
      1. system 里没有 POV 硬规则、没有「第一人称剧情旁白」，旁白人称作答是
         第三人称；
      2. 规则 5 换成第三人称版（不写 pov / pov hands / 1other）；
      3. check：双人稿合格，**POV 泄漏也不拦**（按用户定调交补正 AI 改）；
      4. collect_issues：POV 泄漏报 pov_in_third，双人稿不报；
      5. 补正员 / 修订助手同样换版，且不把双人改回单人。
    """

    SINGLE = ("# 标题：x\n\n## 01\n画面: 1girl, solo, pov, black hair, "
              "white shirt\n气泡: 一\n标签1: 她站在门口\n")
    DOUBLE = ("# 标题：x\n\n## 01\n画面: 1girl, 1boy, black hair, white shirt, "
              "short black hair, black hoodie, bedroom\n"
              "气泡: 一\n标签1: 他站在门口\n")
    LEAK = ("# 标题：x\n\n## 01\n画面: 1girl, solo, pov, pov hands, black hair, "
            "white shirt\n气泡: 一\n标签1: 我把手伸过去\n")

    def test_system_prompt_replaces_every_pov_rule(self):
        t = comic_agent._system_prompt(3, pov_mode="third")
        self.assertIn("【第三人称规则（规则 14–17）开始】", t)
        self.assertNotIn("【POV 视角硬规则", t)
        self.assertNotIn("【自由视角规则", t)
        # 旁白那两处硬编码也必须换掉——不换等于「画面写双人、旁白还是我」
        self.assertNotIn("第一人称剧情旁白", t)
        self.assertIn("第三人称剧情旁白", t)
        self.assertIn("这一话没有镜头外的「我」", t)
        # 手部规则换成第三人称版
        self.assertNotIn("视角方（「我」）的手只在剧情真的需要时才入画", t)
        self.assertIn("画面里有几个人，手就长在他们身上", t)
        # 通用规则一条都不许少
        for kw in ("标签为主 + 允许介词短语补位置", "navy pleated skirt",
                   "一格只能有一个明确地点"):
            self.assertIn(kw, t)

    def test_spec_pov_chapters_cut_in_third(self):
        cut = comic_agent._story_spec_text("third")
        self.assertNotIn("十一、镜头人称", cut)
        self.assertNotIn("全篇第一人称 POV**", cut)
        self.assertIn("四、男女双人场景额外规则", cut)

    def test_check_allows_double_and_pov_leak(self):
        self.assertTrue(comic_agent.check(self.DOUBLE, 1, pov_mode="third")[0])
        # POV 泄漏不判不合格：用户定调交给补正 AI 改，不回炉白烧一稿
        self.assertTrue(comic_agent.check(self.LEAK, 1, pov_mode="third")[0])
        # 同一份双人稿在 POV 档下仍然不合格（老行为没被改动）
        self.assertFalse(comic_agent.check(self.DOUBLE, 1, pov_mode="pov")[0])

    def test_collect_issues_flags_pov_in_third(self):
        kinds = {i["kind"] for i in comic_agent.collect_issues(self.LEAK, pov_mode="third")}
        self.assertIn("pov_in_third", kinds)
        tags = {i["tag"] for i in comic_agent.collect_issues(self.LEAK, pov_mode="third")
                if i["kind"] == "pov_in_third"}
        self.assertEqual(tags, {"pov", "pov_hands"})
        # 双人稿没有这条
        kinds2 = {i["kind"] for i in comic_agent.collect_issues(self.DOUBLE, pov_mode="third")}
        self.assertNotIn("pov_in_third", kinds2)
        # 其它档不产生这条（POV 档本来就该有 pov）
        self.assertNotIn("pov_in_third",
                         {i["kind"] for i in comic_agent.collect_issues(self.SINGLE)})

    def test_pov_tags_in_detects_all_variants(self):
        text = ("# 标题：x\n\n## 01\n画面: 1girl, female_pov, 1other, black hair\n"
                "气泡: 一\n标签1: 甲\n")
        got = comic_agent.pov_tags_in(text)
        self.assertEqual(sorted(t for _, t in got), ["1other", "female_pov"])

    def test_fix_and_chat_prompt_use_third_rules(self):
        for fn in (comic_agent._fix_system_prompt, comic_agent._chat_system_prompt):
            t = fn("third")
            self.assertIn("【第三人称规则", t)
            self.assertNotIn("POV 视角硬规则", t)
            self.assertNotIn("十一、镜头人称", t)
        # 补正员的头号职责也换了——不换它会拿着 POV 规则把双人删成单人
        fix = comic_agent._fix_system_prompt("third")
        self.assertIn("把每一格改回第三人称", fix)
        self.assertIn("不要把双人改成单人", fix)

    def test_write_script_forwards_third(self):
        with mock.patch.object(comic_agent, "run_rounds",
                               return_value=self.DOUBLE) as m:
            comic_agent.write_script("x", panels=1, pov_mode="third")
        system = m.call_args.args[0][0]["content"]
        self.assertIn("【第三人称规则", system)


class LedgerTest(unittest.TestCase):
    """状态账本（2026-10-26）：编剧动笔前先写三行状态，代码抽出来放剧本头部。

    由来：用户 17 格《放学后的器材室》16 格是 `princess carry`（被抱着）→ 17 格
    已经 `sitting in corner`（瘫在墙角），中间缺「被放下」那一拍；01/02 手里的
    扫帚到 03 格凭空消失。前期没交接，后期补正 + 作者一起返工了一个多小时。
    账本的作用是让模型写第 15 格时还有表可查（自回归里先写的 token 会留在上下文）。
    """

    DRAFT = (
        "账本-服饰: 01-05 白针织衫；06 脱掉露白蕾丝胸罩\n"
        "账本-道具: 棕色木扫帚 01-02 她手里，03 掉地上\n"
        "账本-位置: 01 靠门站着\n"
        "\n"
        "# 标题：器材室\n\n## 01\n"
        "画面: safe, 1girl, solo, pov, white knit sweater\n"
        "气泡: （女）嗯\n标签1: 她站在门口\n"
    )

    def test_extracts_three_lines(self):
        rest, ledger = comic_agent._extract_ledger(self.DRAFT)
        self.assertEqual(len(ledger), 3)
        self.assertTrue(ledger[0].startswith("服饰："))
        self.assertNotIn("账本", rest)
        self.assertIn("# 标题：器材室", rest)

    def test_tolerates_format_variants(self):
        """模型会写成 `【账本】-道具：…` / `账本：位置：…`，都得认。"""
        t = ("【账本】-服饰：01 白衬衫\n"
             "账本：道具：白茶杯\n"
             "账本 - 位置: 01 站着\n")
        _, ledger = comic_agent._extract_ledger(t)
        self.assertEqual([x.split("：")[0] for x in ledger], ["服饰", "道具", "位置"])

    def test_no_ledger_keeps_text_untouched(self):
        t = "# 标题：x\n## 01\n画面: a\n"
        rest, ledger = comic_agent._extract_ledger(t)
        self.assertEqual(rest, t)
        self.assertEqual(ledger, [])

    def test_dedupes(self):
        t = "账本-服饰: 甲\n账本-服饰: 甲\n"
        _, ledger = comic_agent._extract_ledger(t)
        self.assertEqual(ledger, ["服饰：甲"])

    def test_plain_line_starting_with_ledger_kept_as_body(self):
        """不是「账本 + 三类关键词」的行不许吃进账本——宁可漏抽，别吃正文。"""
        t = "账本说明：这一话共 8 格\n## 01\n"
        rest, ledger = comic_agent._extract_ledger(t)
        self.assertEqual(ledger, [])
        self.assertIn("账本说明", rest)

    def test_lands_in_head_not_in_panel(self):
        """账本落在第一个 `## NN` 之前（解析器忽略头部），一个字都不进格子。"""
        rest, ledger = comic_agent._extract_ledger(self.DRAFT)
        clean, notes = comic_agent._extract_notes(comic_agent._strip_preamble(rest))
        out = comic_agent._place_notes(clean, comic_agent._with_ledger(notes, ledger))
        head, _, panels = out.partition("## 01")
        self.assertIn("[账本] 服饰：", head)
        self.assertNotIn("账本", panels)
        _, got = comic.parse_script_text(out)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["scene"].strip(),
                         "safe, 1girl, solo, pov, white knit sweater")

    def test_write_script_puts_ledger_in_head(self):
        with mock.patch.object(comic_agent, "run_rounds", return_value=self.DRAFT):
            out = comic_agent.write_script("x", panels=1)
        self.assertIn("[账本] 位置：", out)
        self.assertLess(out.index("[账本]"), out.index("## 01"))


if __name__ == "__main__":
    unittest.main()
