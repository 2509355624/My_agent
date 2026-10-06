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
import random
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
#   - 女仆：通用 tag `maid` 和角色 `maid_(disgaea)` 同名中文——用来钉
#     `_is_generic_key` 的判据
#   - hatsune_miku_(cosplay)（cat=0）：角色派生条目，用来钉通用段必须滤掉它
FIXTURE = (
    "hatsune_miku\t初音未来（VOCALOID）\t4\n"
    "hatsune_miku_(swimwear)\t初音未来（泳装）\t4\n"
    "expo_miku_(2019_taiwan)\t初音未来（2019台湾展）\t4\n"
    "hatsune_miku_(if)\t初音未来（if）\t4\n"
    "hatsune_miku_(cosplay)\t初音未来（Cosplay）\t0\n"
    "silver_wolf_(honkai:_star_rail)\t银狼（崩坏：星穹铁道）\t4\n"
    "ginro_(dr._stone)\t银狼（Dr.STONE）\t4\n"
    "raiden_shogun\t雷电将军（原神）\t4\n"
    "raiden_shogun_(cosplay)\t雷电将军（Cosplay）\t0\n"
    "maid\t女仆\t0\n"
    "maid_(disgaea)\t女仆（魔界战记）\t4\n"
    "girl_(anime_expo)\t女孩（Anime_Expo）\t3\n"
    "the_girl_(resident_evil)\t女孩（生化危机）\t4\n"
    "close-up\t特写\t0\n"
    "twintails\t双马尾\t0\n"
    "twintelle_(arms)\t双马尾（ARMS）\t4\n"
    "genshin_impact\t原神\t3\n"
    "red_dress\t红裙子\t0\n"
    "blue_dress\t蓝裙子\t0\n"
    "shiruhino\tshiruhino\t1\n"
    "vivicat\tVivicat\t1\n"
    "gyaza\tgyaza\t1\n"
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

    def test_query_is_not_required_because_random_mode_has_none(self):
        """`query` 从必填改成可选。

        2026-10-06 用户拍板「search_tags 加 random 参数，画师+角色+通用(带关键词)」
        之后，随机模式本来就没有 query——再钉着 required=["query"] 会逼模型
        传一个空串，有些模型传不好。
        """
        schema = self._schema()
        self.assertIn("query", schema["parameters"]["properties"])
        self.assertEqual(schema["parameters"]["required"], [])

    def test_random_knobs_are_exposed(self):
        props = self._schema()["parameters"]["properties"]
        for k in ("random", "cat", "count", "pattern"):
            self.assertIn(k, props)

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
        """cat=3 的「女孩」不该被滑窗捞出来（真实库里的噪音）。

        ⚠️ 夹具里另有一条 cat=4 的 `the_girl_(resident_evil)`（中文也叫「女孩」），
        那条**本来就该**被滑窗捞到——滑窗只收角色类。所以这里钉的是具体那条
        cat=3 条目不许出现，不是「整句零命中」。
        """
        out = st.search_tags("一个女孩在海边")
        self.assertNotIn("girl_(anime_expo)", out)

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


class ExtractTest(_Base):
    """定点抽取（2026-10-06 用户拍板的主路径）。

    用户原话：「我唯一的目的其实就是 AI 不要犯错……我们一次搜索应该给回多少
    的东西呢？……组合成差不多 500~1000 个字的这样一套东西发给这个生图 API，
    让它去可以参考这个写法。」

    所以 `extract()` 是**代码扫库**：0 token、微秒级、**没有任何编造空间**。
    这组用例钉三件事：
      ① 用户原话里字面出现的词必须被认出来（原来只按角色搜，通用词全漏）；
      ② 认出来的必须是**库里真实存在**的条目；
      ③ 歧义不替用户挑，全列出来交给下游。
    """

    def test_general_words_inside_a_sentence_are_resolved(self):
        """通用类必须走滑窗。

        回归点：`zh_gen` 只做**整串精确查**，所以「来一张特写的双马尾」里的
        「特写」「双马尾」原来一个都查不到——用户抱怨的「它现在只是按照角色
        去进行搜索」就是这个。
        """
        r = st.extract("来一张特写的双马尾")
        self.assertIn("特写", r["confirmed"])
        self.assertIn("close-up", r["doc"])
        self.assertIn("双马尾", r["confirmed"])
        self.assertIn("twintails", r["doc"])

    def test_ambiguous_character_is_flagged_not_decided(self):
        """铁律第 3 条：歧义必问，代码不许替用户挑。"""
        r = st.extract("画个银狼")
        self.assertIn("银狼", r["ambiguous"])
        self.assertNotIn("银狼", r["confirmed"])
        self.assertIn("silver_wolf_(honkai:_star_rail)", r["doc"])
        self.assertIn("ginro_(dr._stone)", r["doc"])

    def test_unique_character_is_confirmed(self):
        r = st.extract("画个雷电将军")
        self.assertIn("雷电将军", r["confirmed"])
        self.assertIn("raiden_shogun", r["doc"])

    def test_character_variant_does_not_pollute_the_general_section(self):
        """`hatsune_miku_(cosplay)`（cat=0）是角色派生条目，不是通用标签。

        回归点：不滤的话「初音未来」会被当成「已查清」，真正的 cat=4 候选
        永远查不出来——**搜索层整个失效**。第一版就是这么错的。
        """
        r = st.extract("画初音未来")
        self.assertIn("初音未来", r["ambiguous"])
        self.assertIn("hatsune_miku", r["doc"])
        self.assertNotIn("hatsune_miku_(cosplay)", r["doc"])

    def test_generic_word_goes_to_the_general_section(self):
        """`女仆` 在库里同时是通用 tag（maid）和一堆角色名（maid_(disgaea)…）。

        正确行为是当**通用词**处理：给出 `maid`，而不是把 9 个无关角色
        摆成「用户点名的角色」。
        """
        r = st.extract("一个女仆")
        self.assertIn("maid", r["doc"])
        self.assertNotIn("maid_(disgaea)", r["doc"])
        self.assertNotIn("女仆", r["ambiguous"])

    def test_short_generic_words_never_become_characters(self):
        """`女孩` 会命中 `the_girl_(resident_evil)`（生化危机）——纯噪音。

        把一个用户根本没提的角色塞进资料包，正是「AI 犯错」的典型来源。
        """
        r = st.extract("画一个女孩")
        self.assertNotIn("the_girl", r["doc"])

    def test_confirmed_and_ambiguous_only_list_words_present_in_the_doc(self):
        """回归：第一版**先统计再卡预算**，结果提示词说「舞台、唱歌已经查过了」，
        而 doc 里根本没有这两行——搜索 agent 会因此跳过它们，凭空丢资料。
        """
        r = st.extract("特写和双马尾和银狼和雷电将军", budget=150)
        self.assertTrue(r["doc"])
        self.assertLessEqual(len(r["doc"]), 150)
        for key in r["confirmed"] + r["ambiguous"]:
            self.assertIn(key, r["doc"])

    def test_unknown_word_yields_empty_doc(self):
        r = st.extract("这个肯定不存在xyzzy")
        self.assertEqual(r["doc"], "")
        self.assertEqual(r["confirmed"], [])
        self.assertEqual(r["ambiguous"], [])

    def test_pure_ascii_need_yields_empty_doc(self):
        # 没有中文就没有滑窗可言；ASCII tag 名交给 search_tags / 搜索 agent
        self.assertEqual(st.extract("silver_wolf")["doc"], "")

    def test_empty_text_is_safe(self):
        self.assertEqual(st.extract("")["doc"], "")
        self.assertEqual(st.extract("   ")["doc"], "")

    def test_doc_respects_the_budget(self):
        r = st.extract("特写和双马尾和银狼和雷电将军和女仆", budget=120)
        self.assertLessEqual(len(r["doc"]), 120)

    def test_random_keyword_produces_real_entries(self):
        r = st.extract("随机画师tag")
        self.assertIn("【随机 画师】", r["doc"])
        # 抽出来的必须是夹具里真实存在的画师
        self.assertTrue(any(a in r["doc"] for a in ("shiruhino", "vivicat", "gyaza")))


class RandomTest(_Base):
    """随机抽样（用户 2026-10-06：「我要一个随机的画师出来，那么它是不是就
    可以？它想要随机，那么我们是不是应该有个随机的工具？」）。

    ⚠️ 通用类**必须带 pattern**——实测均匀抽 cat=0 会抽出 `porsche_997`
    （保时捷）、`bicycle_rack`（自行车架）、`fn_model_1910`（手枪）。
    """

    def test_artist_draw_returns_real_entries(self):
        out = st.random_tags("1", 2, rng=random.Random(0))
        self.assertIn("随机 画师", out)
        hits = [a for a in ("shiruhino", "vivicat", "gyaza") if a in out]
        self.assertEqual(len(hits), 2)

    def test_artist_draw_notes_the_at_prefix(self):
        # SKILL.md：画师 tag 在提示词里要加 @ 前缀
        self.assertIn("@ 前缀", st.random_tags("1", 1, rng=random.Random(0)))

    def test_general_draw_requires_a_pattern(self):
        out = st.random_tags("0", 3)
        self.assertTrue(out.startswith("错误"))
        self.assertIn("pattern", out)

    def test_general_draw_with_pattern(self):
        out = st.random_tags("0", 2, pattern="dress", rng=random.Random(0))
        self.assertIn("red_dress", out)
        self.assertIn("blue_dress", out)
        self.assertNotIn("close-up", out)

    def test_pattern_with_no_match_reports_an_error(self):
        self.assertTrue(st.random_tags("0", 2, pattern="zzzz").startswith("错误"))

    def test_bad_regex_is_rejected(self):
        self.assertTrue(st.random_tags("0", 2, pattern="[(").startswith("错误"))

    def test_unknown_category_is_rejected(self):
        self.assertTrue(st.random_tags("9", 2).startswith("错误"))

    def test_count_is_clamped(self):
        # 硬上限 20；夹具只有 3 个画师，抽 999 也只该拿到 3 个
        out = st.random_tags("1", 999, rng=random.Random(0))
        self.assertIn("均匀抽 3 条", out)

    def test_missing_library_reports_error(self):
        os.remove(os.path.join(self.root, "anima-tags", "data", "tags.tsv"))
        self.assertTrue(st.random_tags("1", 2).startswith("错误"))

    def test_search_tags_random_flag_delegates(self):
        out = st.search_tags(random=True, cat="1", count=2)
        self.assertIn("随机 画师", out)

    def test_search_tags_without_query_tells_you_about_random(self):
        out = st.search_tags()
        self.assertTrue(out.startswith("错误"))
        self.assertIn("random", out)


class DetectRandomTest(_Base):
    """「随机」口令识别。只在用户**明说随机**时才认——没提随机就乱抽，
    等于把用户的具体要求覆盖掉。"""

    def test_artist(self):
        self.assertEqual(st.detect_random("随机画师tag"), ("1", None))
        self.assertEqual(st.detect_random("来个随机的画风"), ("1", None))

    def test_character(self):
        self.assertEqual(st.detect_random("随机角色"), ("4", None))

    def test_general_needs_a_pattern(self):
        cat, pat = st.detect_random("随机服饰")
        self.assertEqual(cat, "0")
        self.assertTrue(pat and "dress" in pat)

    def test_action_and_scene(self):
        self.assertEqual(st.detect_random("随机姿势")[0], "0")
        self.assertEqual(st.detect_random("随机背景")[0], "0")

    def test_no_keyword_means_none(self):
        self.assertIsNone(st.detect_random("画个银狼"))
        self.assertIsNone(st.detect_random(""))
        self.assertIsNone(st.detect_random(None))


if __name__ == "__main__":
    unittest.main()
