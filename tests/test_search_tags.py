# -*- coding: utf-8 -*-
"""search_tags 测试 —— 标签库结构化检索。

这个工具是「搜索 agent」的**唯一**取数口，所以三件事必须钉死：

1. **召回要全**：同一个中文名可能对应几十上百个候选（真实库里「初音未来」
   有 111 个 cat=4 候选），漏掉正确的那个 = 生图画出别人。
2. **不能替用户挑**：`skills/anima-tags/SKILL.md` 铁律第 8 条禁止按热度排序。
   截断只能用「无括号基础 tag 优先」这种**命名结构**规则，不能引入热度信号。
3. **拿不准要说不知道**：查不到就明说查不到，绝不编一个 tag 出来——铁律第 4 条。

索引是按 mtime+size 缓存的，这里也要钉住「文件没动就复用、动了就重建」，
否则改完标签库不重启就一直是旧的。
"""

import os
import tempfile
import unittest
from unittest import mock

from app.tools import sandbox
from app.tools.normal import search_tags as st
from app.tools.registry import TOOLS

# 夹具刻意覆盖真实库里的几种坑：
#   - 初音未来：111 个候选的极端情况，基础 tag hatsune_miku 必须排第一，
#     而字母序会把它挤到 expo_* 后面
#   - 银狼：3 个候选、**没有**无括号基础 tag，纯代码挑不出来
#   - 雷电将军：基础 tag + 一个 cat=0 的 cosplay 变体（滑窗只收 cat=4）
#   - 女孩：cat=3 的噪音，不该被滑窗捞到
FIXTURE = (
    "hatsune_miku\t初音未来（VOCALOID）\t4\n"
    "hatsune_miku_(swimwear)\t初音未来（泳装）\t4\n"
    "expo_miku_(2019_taiwan)\t初音未来（2019台湾展）\t4\n"
    "hatsune_miku_(if)\t初音未来（if）\t4\n"
    "silver_wolf_(honkai:_star_rail)\t银狼（崩坏：星穹铁道）\t4\n"
    "ginro_(dr._stone)\t银狼（Dr.STONE）\t4\n"
    "raiden_shogun\t雷电将军（原神）\t4\n"
    "raiden_shogun_(cosplay)\t雷电将军（Cosplay）\t0\n"
    "girl_(anime_expo)\t女孩（Anime_Expo）\t3\n"
    "close-up\t特写\t0\n"
    "twintails\t双马尾\t0\n"
    "twintelle_(arms)\t双马尾（ARMS）\t4\n"
    "genshin_impact\t原神\t3\n"
)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        p = mock.patch.object(sandbox, "SKILLS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        # 每个用例都从空缓存开始，避免互相污染
        st._INDEX_CACHE.clear()
        self.addCleanup(st._INDEX_CACHE.clear)
        self.write(FIXTURE)

    def write(self, content):
        path = os.path.join(self.root, "anima-tags", "data", "tags.tsv")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path


class RegistryTest(_Base):
    def _schema(self):
        # TOOLS 是 list[dict]，不是 dict
        return {t["name"]: t for t in TOOLS}["search_tags"]

    def test_registered_with_required_query(self):
        schema = self._schema()
        self.assertIn("query", schema["parameters"]["properties"])
        self.assertEqual(schema["parameters"]["required"], ["query"])

    def test_description_warns_against_picking_for_the_user(self):
        # 描述里必须写清「只召回不挑」——搜索 agent 只看得到这段描述
        self.assertIn("不替你挑", self._schema()["description"])


class BasicTest(_Base):
    def test_empty_query_is_rejected(self):
        self.assertTrue(st.search_tags("").startswith("错误"))
        self.assertTrue(st.search_tags("   ").startswith("错误"))

    def test_missing_library_reports_error(self):
        os.remove(os.path.join(self.root, "anima-tags", "data", "tags.tsv"))
        self.assertIn("不存在", st.search_tags("银狼"))

    def test_no_hit_says_so_and_forbids_inventing(self):
        out = st.search_tags("这个角色肯定不存在xyzzy")
        self.assertIn("没有命中", out)
        self.assertIn("不要编造", out)


class ExactAndPrefixTest(_Base):
    def test_exact_tag_hit_returns_chinese(self):
        out = st.search_tags("raiden_shogun")
        self.assertIn("【精确命中 tag】", out)
        self.assertIn("雷电将军（原神）", out)
        self.assertIn("cat=4", out)

    def test_prefix_hit_lists_variants(self):
        out = st.search_tags("hatsune")
        self.assertIn("【tag 前缀命中】", out)
        self.assertIn("hatsune_miku", out)

    def test_exact_hit_wins_over_window(self):
        # 用户直接给完整 tag 名时，不该再去滑窗
        out = st.search_tags("silver_wolf_(honkai:_star_rail)")
        self.assertIn("【精确命中 tag】", out)
        self.assertNotIn("【中文名命中】", out)


class WindowTest(_Base):
    def test_chinese_name_found(self):
        out = st.search_tags("银狼")
        self.assertIn("【中文名命中】", out)
        self.assertIn("silver_wolf_(honkai:_star_rail)", out)
        self.assertIn("ginro_(dr._stone)", out)

    def test_name_inside_a_sentence_is_found(self):
        out = st.search_tags("帮我画一个银狼在打游戏")
        self.assertIn("银狼", out)
        self.assertIn("silver_wolf_(honkai:_star_rail)", out)

    def test_longest_match_wins(self):
        """滑窗必须取最长匹配。

        朴素两层循环会把「雷电将军」碎成「雷电」「将军」两个假命中——这条
        用例就是防那个回归。
        """
        out = st.search_tags("来一张雷电将军的特写")
        self.assertIn("raiden_shogun", out)
        self.assertNotIn("- 雷电 →", out)
        self.assertNotIn("- 将军 →", out)

    def test_series_suffix_is_stripped_for_lookup(self):
        """中文列是「角色名（作品名）」整串，剥掉作品名才查得到。

        不剥的话精确匹配 `\\t银狼\\t` 永远零命中（真实踩过）。
        """
        out = st.search_tags("银狼")
        self.assertIn("2 个候选", out)

    def test_series_suffix_can_also_be_queried_whole(self):
        out = st.search_tags("银狼（崩坏：星穹铁道）")
        self.assertIn("silver_wolf_(honkai:_star_rail)", out)

    def test_non_character_category_is_not_windows_scanned(self):
        """cat=3 的「女孩」不该被滑窗捞出来（真实库里的噪音）。"""
        out = st.search_tags("一个女孩在海边")
        self.assertIn("没有命中", out)

    def test_window_only_returns_character_category(self):
        """滑窗段只收 cat=4。

        cat=0 的 `raiden_shogun_(cosplay)` 可能从**通用索引**那条路出来
        （那是整串精确查，另有一段、另有 cat 标注，不算错），但绝不能混进
        【中文名命中】段——否则角色候选里就掺了非角色。
        """
        out = st.search_tags("雷电将军")
        window = out.split("【中文名命中】")[1].split("【")[0]
        self.assertIn("raiden_shogun\t", window)
        self.assertNotIn("raiden_shogun_(cosplay)", window)
        self.assertNotIn("cat=0", window)

    def test_non_character_chinese_falls_back_to_exact_column_scan(self):
        """通用标签（双马尾 → twintails）走通用索引的整串精确查。"""
        out = st.search_tags("双马尾")
        self.assertIn("twintails", out)
        self.assertIn("通用/作品标签精确命中", out)

    def test_general_lookup_runs_even_when_window_also_hits(self):
        """「双马尾」在真实库里同时是**角色名**（twintelle_(arms)）。

        滑窗会先命中角色，如果通用查只在「全落空」时才跑，真正的
        `twintails` 就被吞掉了——这条就是防那个回归。
        """
        out = st.search_tags("双马尾")
        self.assertIn("twintelle_(arms)", out)   # 角色候选仍在
        self.assertIn("twintails", out)          # 通用标签也必须在

    def test_general_lookup_is_exact_only(self):
        """通用索引只认整串相等，不做滑窗——否则「女孩」又会变成噪音。"""
        out = st.search_tags("一个双马尾的女孩")
        self.assertNotIn("twintails", out)

    def test_copyright_name_is_findable(self):
        out = st.search_tags("原神")
        self.assertIn("genshin_impact", out)


class TruncationTest(_Base):
    def test_base_tag_comes_first(self):
        """截断时基础 tag 必须排前面。

        字母序会把 hatsune_miku 挤到 expo_miku_(2019_taiwan) 后面——
        那样截断就把正确答案砍掉了。
        """
        out = st.search_tags("初音未来")
        lines = [l.strip() for l in out.splitlines() if l.strip().startswith("hatsune")]
        self.assertTrue(lines, "没列出 hatsune_miku")
        body = [l.strip() for l in out.splitlines() if "\t" in l and "cat=" in l]
        self.assertTrue(body[0].startswith("hatsune_miku\t"),
                        "基础 tag 不在第一位: " + body[0])

    def test_overflow_is_reported_not_silently_dropped(self):
        out = st.search_tags("初音未来", max_per_hit=2)
        self.assertIn("另有 2 个变体未列出", out)

    def test_max_per_hit_is_clamped(self):
        # 硬上限 40：给个离谱的值不该把整库倒出来
        out = st.search_tags("初音未来", max_per_hit=99999)
        self.assertLessEqual(len(out), 2000)

    def test_max_hits_caps_sentence_scan(self):
        out = st.search_tags("银狼和初音未来和雷电将军", max_hits=1)
        self.assertIn("未列出", out)


class CacheTest(_Base):
    def test_index_is_rebuilt_when_file_changes(self):
        """标签库改了要热生效，不能一直吃旧索引。"""
        out1 = st.search_tags("银狼")
        self.assertIn("ginro_(dr._stone)", out1)

        self.write(FIXTURE.replace(
            "ginro_(dr._stone)\t银狼（Dr.STONE）\t4\n", ""))
        out2 = st.search_tags("银狼")
        self.assertNotIn("ginro_(dr._stone)", out2)

    def test_index_is_reused_when_file_unchanged(self):
        st.search_tags("银狼")
        self.assertEqual(len(st._INDEX_CACHE), 1)
        with mock.patch.object(st, "_build",
                               side_effect=AssertionError("不该重建")) as m:
            st.search_tags("初音未来")
        self.assertEqual(m.call_count, 0)

    def test_query_does_not_write_anything(self):
        before = sorted(os.listdir(self.root))
        st.search_tags("银狼")
        self.assertEqual(before, sorted(os.listdir(self.root)))


if __name__ == "__main__":
    unittest.main()
