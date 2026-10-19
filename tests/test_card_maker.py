# -*- coding: utf-8 -*-
"""角色卡生成测试（2026-10-19）：解析 / 整形 / 检测 / 存储 / 封面尺寸。

钉死这几件事，都是踩过的：

  1. `_parse` 能从「外面套了一层解释」和「```json 围栏」里抠出 JSON，抠不出来
     就抛错——**不猜、不兜底**（模型没交作业才重跑）。
  2. `_normalize` 只整形不改写：分类是 App 里的封闭选项，造的词过滤掉、原词留在
     `_raw_categories` 里好报警；其余字段原样收。
  3. `check` **只报不拦**，而且不能误报——`{{user}}` 里含 `{user}`，早期版本就是
     这么把合法的变量写法报成错的（2026-10-19 实测踩到）。
  4. 封面参数：864×1536 原尺寸（latent 放大 1.0 + 跳过最终 2x），不是 2458×3686。
  5. 卡库往返：存 / 取 / 改 / 删 / 读坏不炸。

只跑本文件：python -m unittest discover -s tests -p "test_card_maker.py"
"""

import copy
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import card_maker as cm


def _good(**kw):
    """一张各项都合格的卡（改哪项就传哪项）。

    五段都是用真实句式重复到建议区间内——测的是「形状对不对」，不是文案好坏。
    """
    card = {
        "name": "沈念", "gender": "女",
        "categories": ["女性向", "原创", "crush"],
        "_raw_categories": ["女性向", "原创", "crush"],
        "tags": ["傲娇"],
        "greeting": "（她把书包甩到肩上，脚步一顿，回头瞥了一眼）你站这儿干嘛，"
                    "等我？少自作多情，我只是顺路。" * 2,
        "intro": "她嘴比脑子快，被夸第一件事是怼回去，怼完又偷偷瞄一眼对方的表情，"
                 "下次照旧。" * 5,
        "speech": "说话爱用短句，语气冲，常带反问，句尾爱拖一个啊或者吧。" * 4,
        "memory": "从初一开始同班，座位隔了一条过道，借橡皮借了三年，谁都没提过。" * 4,
        "rules": "永远不主动结束对话，不聊学习压力和工作话题，不替对方做决定。" * 4,
        "cover": "1girl, black hair, school uniform",
        "note": "补了名字和年级",
    }
    card.update(kw)
    return card


class ParseTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(cm._parse('{"name":"a"}'), {"name": "a"})

    def test_fenced_json(self):
        got = cm._parse('好的，如下：\n```json\n{"name":"a"}\n```\n希望满意')
        self.assertEqual(got["name"], "a")

    def test_json_buried_in_prose(self):
        self.assertEqual(cm._parse('这是结果 {"name":"b"} 以上')["name"], "b")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            cm._parse("   ")

    def test_not_json_raises(self):
        with self.assertRaises(ValueError):
            cm._parse("我觉得这个角色应该是……")

    def test_json_array_raises(self):
        with self.assertRaises(ValueError):
            cm._parse("[1, 2]")


class NormalizeTest(unittest.TestCase):
    def test_unknown_category_filtered_but_kept_for_report(self):
        card = cm._normalize({"name": "x", "categories": ["原创", "古风"]})
        self.assertEqual(card["categories"], ["原创"])
        self.assertEqual(card["_raw_categories"], ["原创", "古风"])

    def test_category_string_is_split(self):
        card = cm._normalize({"categories": "原创、年上，剧情"})
        self.assertEqual(card["categories"], ["原创", "年上", "剧情"])

    def test_categories_capped_at_five(self):
        six = ["女卡", "男卡", "原创", "剧情", "纯爱", "年上"]
        card = cm._normalize({"categories": six})
        self.assertEqual(len(card["categories"]), cm.MAX_CATEGORIES)
        self.assertEqual(card["_raw_categories"], six)

    def test_tags_string_is_split(self):
        self.assertEqual(cm._normalize({"tags": "甜宠、占有欲"}),
                         cm._normalize({"tags": ["甜宠", "占有欲"]}))

    def test_missing_fields_become_empty_strings(self):
        card = cm._normalize({"name": "x"})
        for key, _l, _h, _r in cm.FIELDS:
            self.assertEqual(card[key], "")

    def test_name_truncated(self):
        self.assertEqual(len(cm._normalize({"name": "长" * 50})["name"]), 20)


class CheckTest(unittest.TestCase):
    def test_good_card_has_no_complaint(self):
        self.assertEqual(cm.check(_good()), [])

    def test_legit_variable_is_not_flagged(self):
        """`{{user}}` 里含 `{user}`——子串判断会误报，这条钉住。"""
        card = _good(greeting=("（她瞥了一眼）%s，你来了，坐吧。" % cm.VAR_USER) * 5)
        self.assertEqual(cm.check(card), [])

    def test_wrong_variable_is_flagged(self):
        card = _good(greeting="（她瞥了一眼）{user}，你来了。")
        self.assertTrue(any("变量写法" in w for w in cm.check(card)))

    def test_player_word_is_flagged(self):
        card = _good(intro="玩家推门进来的时候，她正在擦杯子。" + "别的内容。" * 30)
        self.assertTrue(any("玩家" in w for w in cm.check(card)))

    def test_unknown_category_reported(self):
        card = _good(categories=["原创"], _raw_categories=["原创", "古风"])
        self.assertTrue(any("古风" in w for w in cm.check(card)))

    def test_bad_gender_reported(self):
        self.assertTrue(any("性别" in w for w in cm.check(_good(gender="女性"))))

    def test_empty_name_reported(self):
        self.assertTrue(any("名称" in w for w in cm.check(_good(name=""))))

    def test_short_section_reported(self):
        self.assertTrue(any("开场白" in w for w in cm.check(_good(greeting="你好。"))))

    def test_long_section_reported(self):
        self.assertTrue(any("说话风格" in w
                            for w in cm.check(_good(speech="话。" * 200))))

    def test_empty_cover_reported(self):
        self.assertTrue(any("封面" in w for w in cm.check(_good(cover=""))))

    # ── AI 味配额（借酒馆卡的七病灶，只报不改）──────────

    def test_good_card_has_no_flavor_complaint(self):
        """合格的卡不能被 AI 味检测误伤，否则每张都挂着几条噪声。"""
        warns = cm.check(_good())
        for bad in ("半 X", "AI 腔", "对仗翻转", "具体数字", "解说腔"):
            self.assertFalse(any(bad in w for w in warns), bad)

    def test_too_many_half_words_reported_but_text_untouched(self):
        body = "她退了半步，停了半秒，声音慢了半拍，笑得半寸。" * 3
        card = _good(intro=body)
        warns = [w for w in cm.check(card) if "半 X" in w]
        self.assertTrue(warns)
        self.assertEqual(card["intro"], body)     # 只报，不改文案

    def test_many_figures_reported(self):
        body = "10秒98，0.05秒，18:40，第9天，还有一个多月。" * 3
        self.assertTrue(any("具体数字" in w for w in cm.check(_good(intro=body))))

    def test_repeated_antithesis_reported(self):
        body = "不是怕疼，是怕你看见。不带脏字，全是实话。" * 3
        self.assertTrue(any("对仗翻转" in w for w in cm.check(_good(intro=body))))

    def test_flavor_words_reported(self):
        body = "她眼中闪过一点东西，嘴角勾起，指节泛白。" * 3
        self.assertTrue(any("AI 腔" in w for w in cm.check(_good(intro=body))))

    def test_missing_orientation_reported(self):
        """酒馆那边性向缺失 = 不合格卡；这边只是搜不到，提醒补一个。"""
        card = _good(categories=["原创", "剧情"],
                     _raw_categories=["原创", "剧情"])
        self.assertTrue(any("性向" in w for w in cm.check(card)))


class CoverOptsTest(unittest.TestCase):
    def test_cover_is_864_by_1536_without_upscaling(self):
        """星陨AI 要 9:16。silver 默认 1024×1536 → ×1.2 → 2x = 2458×3686，
        直接拿会裁掉大半，所以 latent 放大调 1.0、最终放大跳过。"""
        opts = cm.cover_opts("silver")
        self.assertEqual((opts["width"], opts["height"]), (864, 1536))
        self.assertEqual(opts["upscale"]["scale_by"], 1.0)
        self.assertTrue(opts["upscale"]["final_skip"])

    def test_cover_steps_are_capped_down(self):
        """20+20 步要 261 秒、10+8 步 24 秒，画质看不出差别（2026-10-19 实测）。"""
        opts = cm.cover_opts("silver")
        self.assertEqual(opts["base"]["steps"], cm.COVER_BASE_STEPS)
        self.assertEqual(opts["hires"]["steps"], cm.COVER_HIRES_STEPS)
        self.assertLessEqual(opts["hires"]["denoise"], cm.COVER_HIRES_DENOISE)

    def test_cover_steps_only_cap_down(self):
        """已经比 10 步少的渠道（moss 那种 4 步）不该被封面逻辑拉高。"""
        from app import comic_render_opts as ro
        fake = {"base": {"steps": 4}, "hires": {"steps": 3, "denoise": 0.2},
                "upscale": {}, "loras": []}
        with mock.patch.object(ro, "defaults", side_effect=lambda *a: copy.deepcopy(fake)):
            opts = cm.cover_opts("moss")
        self.assertEqual(opts["base"]["steps"], 4)
        self.assertEqual(opts["hires"]["steps"], 3)
        self.assertEqual(opts["hires"]["denoise"], 0.2)

    def test_cover_prompt_needs_a_tag(self):
        with self.assertRaises(ValueError):
            cm.cover_prompt({"cover": "  "})
        self.assertEqual(cm.cover_prompt({"cover": " 1girl "}), "1girl")


class CoverSizeTest(unittest.TestCase):
    """封面要的是「就是 864×1536」，不是「按模板比例放大到多大」。

    2026-10-19 实测：batch 族（sd / miao / nsfw_pose_gen）的第二段尺寸是节点里
    写死的数值，`apply()` 会按底图比例**等比缩放**它——864×1536 的底图会出
    1296×2304；`image_gen_v1` 的最终 2x 还在节点内部（`upscale_model`），
    `final_skip` 够不着。两种都得靠 `pin_output_size()` 再钉一次。
    """

    @staticmethod
    def _wf(skill):
        from app import comic_render_opts as ro
        if not ro.caps_of(skill).get("family"):
            raise unittest.SkipTest("没有 %s 的 workflow 模板" % skill)
        return cm.cover_workflow(skill, "1girl, solo", 42)

    @staticmethod
    def _out_size(wf):
        from app import comic_render_opts as ro
        hosts = ro._size_hosts(wf)
        lat = ro._inputs(wf[hosts[0]])
        w0, h0 = lat["width"], lat["height"]
        bh = ro._batch_hires(wf)
        if bh:
            bi = ro._inputs(wf[bh])
            # enable_hires 关掉的渠道（jank）走底图尺寸
            return (bi["hires_width"], bi["hires_height"]) if bi.get("enable_hires") else (w0, h0)
        ups = ro._upscale_nodes(wf)
        sy = ro._inputs(wf[ups["latent"][0]])["scale_by"] if ups["latent"] else 1.0
        return int(round(w0 * sy)), int(round(h0 * sy))

    def test_every_node_family_comes_out_at_cover_size(self):
        """四种形状各挑一个代表：两段 ks / 单段 ks / batch / custom。"""
        from app import comic_render_opts as ro
        from app import image_jobs
        if not hasattr(image_jobs, "NAI_SKILLS"):
            self.skipTest("image_jobs 不可用")
        for skill in ("anima-line", "qwen_image_v1", "miao", "moss",
                      "image_gen_v1", "nsfw_pose_gen", "jank"):
            with self.subTest(skill=skill):
                self.assertEqual(self._out_size(self._wf(skill)), (cm.COVER_W, cm.COVER_H))

    def test_batch_hires_is_pinned_not_scaled(self):
        """miao 模板是 1024×1536→1536×2304（×1.5）。等比跟随会算出
        1296×2304，尺寸对了但宽高比还是被拉——必须是同尺寸精修。"""
        from app import comic_render_opts as ro
        wf = self._wf("miao")
        bi = ro._inputs(wf[ro._batch_hires(wf)])
        self.assertEqual((bi["hires_width"], bi["hires_height"]), (cm.COVER_W, cm.COVER_H))

    def test_node_internal_upscaler_is_disconnected(self):
        """image_gen_v1 的 2x 在节点输入里，不删就是 1728×3072。"""
        from app import comic_render_opts as ro
        wf = self._wf("image_gen_v1")
        self.assertNotIn("upscale_model", ro._inputs(wf[ro._batch_hires(wf)]))

    def test_save_image_does_not_go_through_the_upscaler(self):
        from app import comic_render_opts as ro
        wf = self._wf("anima-line")
        final = str(ro._upscale_nodes(wf)["final"])
        for nd in wf.values():
            if nd.get("class_type") == "SaveImage":
                self.assertNotEqual(str(ro._inputs(nd)["images"][0]), final)

    def test_hires_stays_off_when_the_channel_disabled_it(self):
        """jank 的 enable_hires=False——别替它开第二段，白烧一倍时间。"""
        from app import comic_render_opts as ro
        wf = self._wf("jank")
        self.assertFalse(ro._inputs(wf[ro._batch_hires(wf)]).get("enable_hires"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._backup = None
        fp = cm.cards_path()
        if os.path.isfile(fp):
            with open(fp, "rb") as f:
                self._backup = f.read()

    def tearDown(self):
        fp = cm.cards_path()
        if self._backup is None:
            if os.path.isfile(fp):
                os.remove(fp)
        else:
            with open(fp, "wb") as f:
                f.write(self._backup)

    def test_roundtrip(self):
        rec = cm.save_card(_good())
        self.assertTrue(rec["id"].startswith("K"))
        got = cm.get_card(rec["id"])
        self.assertEqual(got["name"], "沈念")
        self.assertEqual(got["categories"], ["女性向", "原创", "crush"])
        self.assertEqual(got["created"], got["updated"])

    def test_update_keeps_created(self):
        rec = cm.save_card(_good())
        created = rec["created"]
        upd = cm.update_card(rec["id"], {"cover_state": "done"})
        self.assertEqual(upd["cover_state"], "done")
        self.assertEqual(upd["created"], created)

    def test_update_unknown_id_returns_none(self):
        self.assertIsNone(cm.update_card("nope", {"cover_state": "done"}))

    def test_delete(self):
        rec = cm.save_card(_good())
        self.assertTrue(cm.delete_card(rec["id"]))
        self.assertFalse(cm.delete_card(rec["id"]))
        self.assertIsNone(cm.get_card(rec["id"]))

    def test_broken_file_falls_back_to_empty(self):
        os.makedirs(os.path.dirname(cm.cards_path()), exist_ok=True)
        with open(cm.cards_path(), "w", encoding="utf-8") as f:
            f.write("{oops")
        self.assertEqual(cm.load_cards(), [])
        self.assertIsNone(cm.get_card("K1"))

    def test_duplicate_timestamp_gets_a_suffix(self):
        """同一秒建两张卡：第二张的 id 加 -2，不能覆盖第一张。"""
        with mock.patch.object(cm.time, "strftime", return_value="20261019-000000"):
            first = cm.save_card(_good(), "K20261019-000000")
            second = cm._new_id()
        self.assertEqual(first["id"], "K20261019-000000")
        self.assertEqual(second, "K20261019-000000-2")
        cm.delete_card(first["id"])

    def test_cover_paths_live_under_the_card(self):
        self.assertTrue(cm.cover_path("K1").endswith(os.path.join("K1", "cover.png")))
        self.assertIn("xingyun_cards", cm.card_dir("K1"))


class DraftTest(unittest.TestCase):
    def test_draft_uses_the_model_and_shapes_the_result(self):
        payload = json.dumps({
            "name": "沈念", "gender": "女", "categories": ["原创", "不存在"],
            "tags": "傲娇、青梅", "greeting": "g", "intro": "i", "speech": "s",
            "memory": "m", "rules": "r", "cover": "1girl", "note": "补了名字",
        }, ensure_ascii=False)
        with mock.patch.object(cm.llm, "call_llm", return_value=payload) as m:
            card = cm.draft("一个傲娇的女高中生")
        self.assertEqual(card["name"], "沈念")
        self.assertEqual(card["categories"], ["原创"])
        self.assertEqual(card["tags"], ["傲娇", "青梅"])
        self.assertEqual(m.call_count, 1)
        # 一次调用出全部字段（五段要语气一致，不拆成五次）
        msgs = m.call_args.args[0]
        self.assertEqual(len(msgs), 2)
        self.assertIn("一个傲娇的女高中生", msgs[1]["content"])

    def test_empty_brief_is_rejected_before_calling(self):
        with mock.patch.object(cm.llm, "call_llm") as m:
            with self.assertRaises(ValueError):
                cm.draft("   ")
        self.assertEqual(m.call_count, 0)

    def test_system_prompt_keeps_the_field_boundaries(self):
        """六个模块的分工照星陨AI 官方教程，提示词里必须留着边界：

        简介只写「基础设定 + 初始关系」，往事归背景记忆；说话风格只写
        「怎么说」；补充规则才是防 OOC 的那一栏。
        """
        self.assertIn("初始关系", cm._SYSTEM)
        self.assertIn("全部放到背景记忆", cm._SYSTEM)
        self.assertIn("只写「怎么说」", cm._SYSTEM)
        self.assertIn("OOC", cm._SYSTEM)
        # 名字：正式姓名，不是小名（2026-10-19 用户点名，参考酒馆社区卡）
        self.assertIn("正式姓名，不是小名", cm._SYSTEM)
        self.assertIn("沈书言", cm._SYSTEM)
        self.assertIn("称号·姓名", cm._SYSTEM)
        # 脏草稿里的昵称前缀不能被当成角色名（2026-10-19 实测踩到）
        self.assertIn("聊天昵称前缀", cm._SYSTEM)
        for name in ("name", "greeting", "intro", "speech", "memory", "rules",
                     "cover", "note"):
            self.assertIn('"%s"' % name, cm._SYSTEM)
        for cat in cm.CATEGORIES:
            self.assertIn(cat, cm._SYSTEM)

    def test_prompt_names_every_section_like_the_tutorial(self):
        """界面上的栏目名得和教程一字不差，不然用户对不上号。"""
        labels = [label for _k, label, _h, _r in cm.FIELDS]
        self.assertEqual(labels, ["开场白", "人物简介", "说话风格", "背景记忆", "补充规则"])
        for label in labels:
            self.assertIn("/ " + label, cm._SYSTEM)

    def test_prompt_takes_the_tavern_four_tuple(self):
        """酒馆卡的「四元组选型」：性向落分类，赛道/洁度/钩子落标签。"""
        for word in ("性向", "赛道", "洁度", "情绪钩子"):
            self.assertIn(word, cm._SYSTEM)
        # 洁度是用户偏好，不许替他拍板
        self.assertIn("用户提了才写", cm._SYSTEM)
        self.assertIn("一律写进 tags", cm._SYSTEM)

    def test_prompt_forbids_ai_flavor(self):
        """七病灶的配额得写进提示词，不然模型还是段段金句。"""
        self.assertIn("均匀的机灵 = AI", cm._SYSTEM)
        self.assertIn("闲笔", cm._SYSTEM)
        self.assertIn("伪精确数字", cm._SYSTEM)
        self.assertIn("生造概念词", cm._SYSTEM)
        # 补充规则本身就是给模型的规格，是这七条的唯一例外
        self.assertIn("不受上面七条约束", cm._SYSTEM)

    def test_prompt_asks_for_a_causal_chain(self):
        """性格不能是形容词表：trait → why → 看得见的行为。"""
        self.assertIn("是什么 → 为什么 → 看得见的行为", cm._SYSTEM)
        self.assertIn("没解决的张力", cm._SYSTEM)

    def test_chat_prefix_looking_name_is_reported(self):
        """「大大怪：」这种是被当成名字的聊天前缀——只提醒，不改写。"""
        for bad in ("233：", "大大怪:", "@大大怪", "233"):
            with self.subTest(name=bad):
                warns = cm.check(_good(name=bad))
                self.assertTrue(any("不像是名字" in w for w in warns), bad)

    def test_pet_name_is_reported(self):
        """「小满」「阿白」是小名；社区卡清一色是完整姓名（江叙白 / 林知夏）。"""
        for bad in ("小满", "阿白", "老张"):
            with self.subTest(name=bad):
                warns = cm.check(_good(name=bad))
                self.assertTrue(any("小名" in w for w in warns), bad)

    def test_full_name_passes(self):
        for good in ("沈书言", "林知夏", "江叙白", "林幺", "欧阳明月"):
            with self.subTest(name=good):
                self.assertEqual([w for w in cm.check(_good(name=good))
                                  if "角色名称" in w], [])


if __name__ == "__main__":
    unittest.main()
