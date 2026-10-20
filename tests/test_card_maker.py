# -*- coding: utf-8 -*-
"""角色卡生成测试（2026-10-19）：解析 / 整形 / 检测 / 存储 / 封面尺寸。

钉死这几件事，都是踩过的：

  1. `_parse` 能从「外面套了一层解释」和「```json 围栏」里抠出 JSON，抠不出来
     就抛错——**不猜、不兜底**（模型没交作业才重跑）。
  2. `_normalize` 只整形不改写：分类是 App 里的封闭选项，造的词过滤掉、原词留在
     `_raw_categories` 里好报警；其余字段原样收。
  3. `check` **只报不拦**，而且不能误报——`{{user}}` 里含 `{user}`，早期版本就是
     这么把合法的变量写法报成错的（2026-10-19 实测踩到）。
  4. 封面参数 = 渠道默认（尺寸 / 放大 / 步数一个都不覆盖）。10-19 曾钉成
     864×1536 省时间，10-20 用户实测糊，改回来了。
  5. 卡库往返：存 / 取 / 改 / 删 / 读坏不炸。

只跑本文件：python -m unittest discover -s tests -p "test_card_maker.py"
"""

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
        # 1 个主锚点写深 + 3 个物件型短锚点（2026-10-19 加的「锚点」规范）
        "memory": "初一开学那天她把新橡皮掰成两半，一半塞给他，说用过的不还，"
                  "那半块他用了三年。她的笔袋里一直留着半块没拆的橡皮。"
                  "运动会那天下大雨，她把伞往他那边偏，自己湿了半条袖子。"
                  "她记得他不吃香菜，每次打饭都先把菜里的挑出来。"
                  "后来她把那半块橡皮缝进校服内袋，洗衣服时掉出来过一次。"
                  "毕业照她站在他斜后方，眼睛看的是镜头外面。",
        # 五段：核心性格 / 节奏锁 / 双面锁 / 尺度锁 / 运行纪律
        "rules": "嘴比脑子快，被夸先怼回去再偷偷看对方反应；"
                 "被表白不许一步到位答应，要先愣住、再当成玩笑、再红着耳朵岔开，"
                 "最后才慢慢认；"
                 "外面那层冲和私底下的慌必须同时在场，不许只演一面；"
                 "亲密只到牵手、并肩、被摸头，越界会先退一步再顶回来；"
                 "被问设定就装傻岔开，不复读同一句口头禅，称呼固定；"
                 "每轮先接对方的话再写动作，用第三人称写神态；"
                 "心里慌得厉害也只写在动作里，嘴上永远不认；"
                 "不主动结束对话，也不替对方做决定，更不自己找台阶下。",
        # 玩法建议：给玩家挑路用，不粘进卡（星陨只有六个框）
        "routes": [
            "顺着现在的节奏往下走，你退一步她反而会凑上来问你怎么了。",
            "谁都不肯先说破，那就看谁先松口，她能用嘴硬撑到最后。",
            "借还东西的日常里慢慢把距离磨近，什么都不用说破。",
        ],
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
        # 玩法建议不在六个框里，但缺了也得给个空列表，别让下游拿到 None
        self.assertEqual(card["routes"], [])

    def test_routes_capped(self):
        card = cm._normalize({"routes": ["建议%d" % i for i in range(9)]})
        self.assertEqual(len(card["routes"]), cm.ROUTES_MAX)

    def test_routes_string_is_split_by_line(self):
        card = cm._normalize({"routes": "第一条\n第二条\n"})
        self.assertEqual(card["routes"], ["第一条", "第二条"])

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

    # ── 锚点 / 玩法建议（2026-10-19 加的规范）────────────

    def test_single_scene_memory_reported(self):
        """只写一个场景就报：规范是 1 个主锚点 + 3~5 个短锚点。"""
        card = _good(memory="她第一次见你时把简历递反了，脸涨得通红" * 3)
        self.assertTrue(any("短锚点" in w for w in cm.check(card)))

    def test_anchored_memory_not_reported(self):
        self.assertFalse(any("短锚点" in w for w in cm.check(_good())))

    def test_few_routes_reported(self):
        card = _good(routes=["一条怎么够，至少给两条路。"])
        self.assertTrue(any("玩法建议" in w for w in cm.check(card)))

    def test_route_length_reported(self):
        long_one = "这条建议写得实在太长了" * 10
        card = _good(routes=[long_one, _good()["routes"][0]])
        self.assertTrue(any("玩法建议" in w for w in cm.check(card)))

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
    def test_cover_keeps_the_channel_size_and_upscale(self):
        """10-20 用户实测：864×1536 只有 1.3 MP，构图发糊，不如渠道默认
        1024×1536 → ×1.2 → 2x = 2458×3686。所以尺寸与放大一律不覆盖。"""
        from app import comic_render_opts as ro
        opts = cm.cover_opts("silver")
        d = ro.defaults("silver")
        self.assertEqual((opts["width"], opts["height"]), (d["width"], d["height"]))
        self.assertEqual(opts["upscale"]["scale_by"], d["upscale"]["scale_by"])
        self.assertEqual(opts["upscale"]["final_skip"], d["upscale"]["final_skip"])

    def test_cover_does_not_touch_sampler_steps(self):
        """压步数是 864×1536 时代的省时间手段，现在不压了：参数逐字等于模板。"""
        from app import comic_render_opts as ro
        opts = cm.cover_opts("silver")
        d = ro.defaults("silver")
        self.assertEqual(opts["base"], d["base"])
        self.assertEqual(opts["hires"], d["hires"])

    def test_cover_opts_never_mutates_the_shared_defaults(self):
        """两次调用必须一致——早前那版是在 defaults() 的返回值上原地改。"""
        from app import comic_render_opts as ro
        cm.cover_opts("silver")
        self.assertEqual(cm.cover_opts("silver")["width"], ro.defaults("silver")["width"])

    def test_cover_prompt_needs_a_tag(self):
        with self.assertRaises(ValueError):
            cm.cover_prompt({"cover": "  "})
        self.assertEqual(cm.cover_prompt({"cover": " 1girl "}), "1girl")


class CoverWorkflowTest(unittest.TestCase):
    """封面工作流 = 渠道自己的单图工作流，一个参数都不改。

    10-19 那套「钉死 864×1536 底图 + 拔掉节点内 2x」的补丁已经撤了：它以画质
    换时间，10-20 用户实测后否掉。这里钉住的是「封面不再对模板动手脚」。
    """

    SKILLS = ("anima-line", "qwen_image_v1", "miao", "moss",
              "image_gen_v1", "nsfw_pose_gen", "jank")

    def test_cover_workflow_is_the_channel_workflow(self):
        from app import comic_render_opts as ro
        from app.tools.normal import generate_image
        for skill in self.SKILLS:
            with self.subTest(skill=skill):
                if not ro.caps_of(skill).get("family"):
                    continue                     # 这台机器没装这个渠道
                cover = cm.cover_workflow(skill, "1girl, solo", 42)
                plain = generate_image.build_t2i_workflow(skill, "1girl, solo", 42)
                self.assertEqual(cover, plain)

    def test_cover_workflow_carries_the_seed(self):
        """换种子必须真变——封面重画靠的就是这一个入参。"""
        a = cm.cover_workflow("silver", "1girl", 1)
        b = cm.cover_workflow("silver", "1girl", 2)
        self.assertNotEqual(a, b)


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
