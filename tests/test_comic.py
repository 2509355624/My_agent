# -*- coding: utf-8 -*-
"""漫画管道测试：气泡2 解析 / 两段渲染 / 编剧规范注入。

2026-10-11 加。背景：用户给了一份《连续剧情提示词生成规范》要求**全文**
喂给编剧 AI（单次请求，不做压缩合并），台词丰富化靠新增 `气泡2:` 字段。
这份测试钉死三件事：
  1. `气泡2:` 被正确解析，且不会因正则交替顺序被「气泡」吃掉；
  2. render_bubble **默认一遍画完两个气泡+旁白**（2026-10-11 合并，省一次
     i2i）；`split=True` 才走老的分两遍——那是画崩后的退路；
  3. 编剧 system prompt 里含规范全文与契约映射，气泡2 超长会被打回。
"""

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import comic, comic_writer, skills


def _panel_dict(no, **kw):
    d = {"no": no, "scene": "1girl, solo, test", "bubble": "台词",
         "bubble2": "", "label": ""}
    d.update(kw)
    return d


class ParseBubble2Test(unittest.TestCase):
    """`气泡2:` 字段解析——正则交替顺序的坑就在这里。"""

    def test_bubble2_parsed(self):
        text = "# 标题：测试\n\n## 01\n画面: 1girl, solo, silver hair\n" \
               "气泡: 第一句\n气泡2: 第二句\n标签: 好感度 0\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(len(panels), 1)
        p = panels[0]
        self.assertEqual(p["bubble"], "第一句")
        self.assertEqual(p["bubble2"], "第二句")
        self.assertEqual(p["label"], "好感度 0")

    def test_bubble2_not_eaten_by_bubble(self):
        # 「气泡2」必须在交替里排在「气泡」前面，否则值会剩成 "2: 第二句"
        text = "## 01\n画面: x\n气泡: 一\n气泡2: 二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二")
        self.assertNotIn("2", panels[0]["bubble"])

    def test_bubble2_continuation_lines(self):
        # 气泡2 也支持续行（契约里字段都可续行）
        text = "## 01\n画面: x\n气泡: 一\n气泡2: 二\n继续二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二 继续二")

    def test_alias_taici2(self):
        text = "## 01\n画面: x\n气泡: 一\n台词2: 二\n"
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["bubble2"], "二")

    def test_no_bubble2_defaults_empty(self):
        _, panels = comic.parse_script_text("## 01\n画面: x\n气泡: 一\n")
        self.assertEqual(panels[0]["bubble2"], "")


class TwoPassBubbleTest(unittest.TestCase):
    """render_bubble：默认一遍画完两个气泡+旁白；split=True 才分两遍。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.submits = []

        def _upload(path):
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded):
            return {"prompt": prompt}

        def _submit(wf, skill, prompt, seed):
            self.submits.append(prompt)
            return "out_%d.png" % len(self.submits)

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"png")

        for target, repl in (("_upload_source", _upload),
                             ("_build_i2i", _build),
                             ("_submit", _submit),
                             ("_download", _download)):
            p = mock.patch.object(comic, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _task(self, bubble2="", label=""):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n"
        if label:
            text += "标签: %s\n" % label
        if bubble2:
            text += "气泡2: %s\n" % bubble2
        path = os.path.join(self.dir, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        os.makedirs(task.final_dir, exist_ok=True)   # mock 的 _download 不做 makedirs
        return task

    def test_single_bubble_one_pass(self):
        task = self._task()
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1)
        self.assertEqual(p.state, "done")
        self.assertTrue(os.path.exists(task.final_path(p)))

    def test_bubble2_one_pass_by_default(self):
        """有第二句台词也**一次画完**——这是 2026-10-11 合并掉第二次 i2i 的核心。

        合并前是「第一遍气泡1+旁白，第二遍在成图上补气泡2」，每个这样的格多花
        一次 50~70 秒。合并的依据：当初会崩是因为模板没声明条数和归属，现在每条
        气泡都声明「文字 + 尾巴指向 + 位置」，真跑 3/3 全对、热机耗时不变。
        """
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1, "默认必须只提交一次")
        self.assertIn("第一句", self.submits[0])
        self.assertIn("第二句", self.submits[0])
        self.assertEqual(p.state, "done")

    def test_merged_prompt_declares_both_bubbles_explicitly(self):
        """合并版必须把**每个**气泡的文字、位置、尾巴指向都写死。

        含糊就挂错人（用户原话「箭头指向谁必须是确定的」）——当初「一次只画一个」
        的取舍正是为了绕开这份含糊，现在两件事可以同时满足。
        """
        p = comic.Panel("01", "1girl, solo", "走吧", "车门合上了",
                        "回家再说", speaker="女", speaker2="我")
        prompt = p.bubble_prompt()
        self.assertIn('"走吧"', prompt)
        self.assertIn('"回家再说"', prompt)
        self.assertIn("左上方的对话框", prompt)
        self.assertIn("右下角的对话框", prompt)          # 旁白占了右上角，它让位
        self.assertIn("尾巴指向画面中的女生", prompt)     # 气泡1 的尾巴
        self.assertIn("尾巴向下指向画面下缘", prompt)      # 气泡2 的尾巴（镜头外的「我」）
        self.assertIn("车门合上了", prompt)             # 旁白也在这一遍里

    def test_merged_prompt_count_is_not_contradictory(self):
        """合并版里不能出现「只有一个气泡」这种自相矛盾的句子。

        `_label_sec` 原来把数量约束写死成「左上角一个气泡」——合并版直接复用，
        同一段提示词就会一边说一个、一边说两个，模型只能二选一。
        （同类前科：提示词里写「`stepping off` 用 `standing on one leg` 之类」，
        模型逐字执行、画成单脚站立的芭蕾姿。）
        """
        p = comic.Panel("01", "1girl, solo", "走吧", "旁白一\n旁白二",
                        "回家再说", speaker="女", speaker2="我")
        prompt = p.bubble_prompt()
        self.assertNotIn("左上角一个气泡", prompt)
        self.assertIn("左上方的那个气泡", prompt)
        self.assertIn("右下角的那个气泡", prompt)         # 旁白占了右上角，它让位
        self.assertIn("画面里的文字一共这三处", prompt)

    def test_split_flag_keeps_the_old_two_passes(self):
        """split=True 是退路：某一格一遍画崩了，单独切回两遍重跑。"""
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p, split=True)
        self.assertEqual(len(self.submits), 2)
        self.assertIn("第一句", self.submits[0])
        self.assertNotIn("第二句", self.submits[0])      # 第一遍只管一个气泡
        self.assertIn("第二句", self.submits[1])
        # 第二遍模板必须按「图里已有第一个气泡」写约束
        self.assertIn("已经有一个对话框", self.submits[1])

    def test_split_does_not_add_a_pass_without_bubble2(self):
        """没有第二句台词时 split 不该凭空多跑一遍。"""
        task = self._task()
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p, split=True)
        self.assertEqual(len(self.submits), 1)

    def test_bubble2_moves_to_bottom_right_with_narration(self):
        """旁白条占着右上角，第二个气泡必须让位到右下角。

        2026-10-11：`标签:` 改成每格必写的剧情旁白后，它和第二个气泡的默认
        位置都是右上角——照原样会把两个文字元素挤在同一个角落。
        """
        task = self._task(bubble2="第二句", label="我攥住了她的手腕")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertEqual(len(self.submits), 1)
        self.assertIn("右下角", self.submits[0])
        self.assertIn("右上角的一行旁白", self.submits[0])     # 右上角留给了旁白

    def test_bubble2_stays_top_right_without_narration(self):
        task = self._task(bubble2="第二句")
        p = task.panels[0]
        os.makedirs(task.raw_dir)
        with open(task.raw_path(p), "wb") as f:
            f.write(b"raw")
        comic.render_bubble(task, p)
        self.assertIn("右上角", self.submits[0])


class ManualRenderTest(unittest.TestCase):
    """render_manual：手动改字。

    源图必须是**当前成品**（final）——用户的实际用法是「在最后那张加了字幕的
    图上，告诉 qwen 某个字糊了、把它重画成 XX」，所以这一遍要能累积着改。
    只有在还没有成品（只有底图）时才退到底图，这样手动入口对「待加气泡」的格
    也能用。改坏了有回退路：拿剧本提示词从底图重画（/api/comic/bubble）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.seen = {}

        def _upload(path):
            self.seen["src"] = path
            return "uploaded.png"

        def _build(skill, prompt, seed, uploaded, denoise=comic.I2I_DENOISE):
            self.seen["prompt"] = prompt
            self.seen["denoise"] = denoise
            return {"prompt": prompt}

        def _submit(wf, skill, prompt, seed):
            return "out.png"

        def _download(name, dst):
            with open(dst, "wb") as f:
                f.write(b"NEW")

        for target, repl in (("_require_comfy", lambda: None),
                             ("_upload_source", _upload),
                             ("_build_i2i", _build),
                             ("_submit", _submit),
                             ("_download", _download)):
            p = mock.patch.object(comic, target, repl)
            p.start()
            self.addCleanup(p.stop)

    def _task(self):
        text = "## 01\n画面: 1girl, solo, test\n气泡: 第一句\n标签1: 甲\n"
        path = os.path.join(self.dir, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        os.makedirs(task.raw_dir, exist_ok=True)
        os.makedirs(task.final_dir, exist_ok=True)
        p = task.panels[0]
        with open(task.raw_path(p), "wb") as f:
            f.write(b"RAW")
        return task, p

    def test_source_is_the_current_final(self):
        task, p = self._task()
        with open(task.final_path(p), "wb") as f:
            f.write(b"OLDFINAL")
        comic.render_manual(task, p, "把「松手啦」重画清楚")
        self.assertEqual(self.seen["src"], task.final_path(p))
        with open(task.final_path(p), "rb") as f:
            self.assertEqual(f.read(), b"NEW")
        self.assertEqual(p.state, "done")

    def test_falls_back_to_raw_when_no_final_yet(self):
        task, p = self._task()
        comic.render_manual(task, p, "加个气泡")
        self.assertEqual(self.seen["src"], task.raw_path(p))

    def test_raises_when_there_is_no_image_at_all(self):
        task, p = self._task()
        os.remove(task.raw_path(p))
        with self.assertRaises(RuntimeError) as cm:
            comic.render_manual(task, p, "改")
        self.assertIn("还没有任何图", str(cm.exception))

    def test_no_tmp_file_left_behind(self):
        """先落 .tmp 再原子替换——半张图写坏就等于把上一版好成品丢了。"""
        task, p = self._task()
        with open(task.final_path(p), "wb") as f:
            f.write(b"OLD")
        comic.render_manual(task, p, "改")
        self.assertEqual(os.listdir(task.final_dir), ["01_final.png"])

    def test_instruction_comes_first_then_the_keep_note(self):
        task, p = self._task()
        comic.render_manual(task, p, "  把「疼」改成「痛」  ")
        prompt = self.seen["prompt"]
        self.assertTrue(prompt.startswith("把「疼」改成「痛」"))
        self.assertIn(comic.MANUAL_KEEP_NOTE, prompt)

    def test_denoise_override_reaches_the_workflow(self):
        task, p = self._task()
        comic.render_manual(task, p, "改", denoise="0.35")
        self.assertEqual(self.seen["denoise"], "0.35")

    def test_denoise_defaults_to_pipeline_value(self):
        task, p = self._task()
        comic.render_manual(task, p, "改")
        self.assertEqual(self.seen["denoise"], comic.I2I_DENOISE)

    def test_state_is_persisted_so_the_panel_shows_done(self):
        task, p = self._task()
        comic.render_manual(task, p, "改")
        again = comic.Task(task.script_path, *comic.parse_script(task.script_path))
        again.load_state()
        self.assertEqual(again.panels[0].state, "done")


class StateRoundtripTest(unittest.TestCase):
    """bubble2 进出 state.json；改 bubble2 让 done 格退回重加气泡。"""

    def test_change_bubble2_resets_done(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = "## 01\n画面: 1girl, solo, test\n气泡: 一\n气泡2: 二\n"
        path = os.path.join(tmp.name, "script.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        meta, panels = comic.parse_script(path)
        task = comic.Task(path, meta, panels)
        p = task.panels[0]
        p.state, p.seed = "done", 42
        task.save_state()

        # 剧本里 bubble2 改了 → 重新解析后 load_state 应把 done 退回 rendered
        text2 = text.replace("气泡2: 二", "气泡2: 改过的二")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text2)
        meta2, panels2 = comic.parse_script(path)
        task2 = comic.Task(path, meta2, panels2)
        task2.load_state()
        self.assertEqual(task2.panels[0].state, "rendered")

        # 没改 → 保持 done
        meta3, panels3 = comic.parse_script(path)
        task3 = comic.Task(path, meta3, panels3)
        task3.load_state()
        self.assertEqual(task3.panels[0].state, "rendered")  # 上一步已退回


class WriterSpecTest(unittest.TestCase):
    """编剧 system prompt 必须含规范全文；气泡2 超长被打回。"""

    def test_system_prompt_contains_full_spec(self):
        prompt = comic_writer._system_prompt(3)
        # 规范全文的标志性段落必须原样在场（用户拍板：不做压缩合并）
        for marker in ("连续剧情提示词生成规范",
                       "动作位置副词规则",
                       "局部动作与表情互斥规则",
                       "girl holding a gift box in front of her chest",
                       "气泡2"):
            self.assertIn(marker, prompt)

    def test_validate_rejects_long_bubble2(self):
        text = ("## 01\n画面: 1girl, solo, test\n气泡: 一\n"
                "气泡2: " + "长" * 17 + "\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("气泡2", why)

    def test_validate_accepts_bubble2(self):
        text = ("## 01\n画面: 1girl, solo, test\n气泡: 一\n气泡2: 二\n"
                "标签: 我没敢松手\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_write_script_passes_spec_via_llm_messages(self):
        good = ("# 标题：测试\n\n## 01\n画面: 1girl, solo, test\n气泡: 一\n"
                "标签: 她拆开了盒子\n")
        with mock.patch.object(comic_writer.llm, "call_llm",
                               return_value=good) as m_llm:
            comic_writer.write_script("妹妹收到礼物", panels=1)
        system = m_llm.call_args.args[0][0]["content"]
        self.assertIn("连续剧情提示词生成规范", system)
        self.assertIn("======== 规范全文开始 ========", system)

    def test_label_prompt_does_not_say_small_font(self):
        """「小字」对 qwen 就是个字号指令——原文写了它，就真画成很小的字。

        2026-10-11 用户反馈「作为标签的字体有点小」：模板原话是「添加一行**小**
        字」。字号必须显式写清楚，不能留这种会被当指令执行的形容词。
        旁白段后来改由 `_label_sec()` 现拼（单条 / 多条措辞不同），所以这里
        查的是**真正发给 qwen 的那段提示词**，而不是模板字面量。
        """
        prompt = comic.Panel("01", "1girl, solo", "台词", "旁白").bubble_prompt()
        self.assertNotIn("小字", prompt)
        self.assertIn("字号清晰可读", prompt)
        self.assertIn('这行旁白写 "旁白"', prompt)


class MultiLabelTest(unittest.TestCase):
    """多条旁白：`标签1:` `标签2:` `标签3:` 按出现顺序各占一行。

    用户 2026-10-11 拍板：一格要能讲好几段剧情（「我带她回了家 / 安慰了她 /
    她的手在袖子里攥紧了」），所以 label 从单值变成多行字符串。
    """

    def test_numbered_labels_become_lines(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 妹妹生闷气离家出走\n标签2: 一只脚已经踩上车门\n")
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["label"],
                         "妹妹生闷气离家出走\n一只脚已经踩上车门")

    def test_bare_label_and_numbered_mix(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 一\n标签: 前情\n标签2: 当下\n")
        self.assertEqual(panels[0]["label"], "前情\n当下")

    def test_three_labels_kept_in_order(self):
        text = ("## 01\n画面: 1girl\n气泡: 一\n"
                "标签1: 我带她回了家\n标签2: 安慰了她\n标签3: 她的手攥紧了\n")
        _, panels = comic.parse_script_text(text)
        self.assertEqual(panels[0]["label"].split("\n"),
                         ["我带她回了家", "安慰了她", "她的手攥紧了"])

    def test_labels_do_not_leak_into_bubble(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 一\n标签1: 甲\n标签2: 乙\n")
        self.assertEqual(panels[0]["bubble"], "一")

    def test_panel_defaults_speaker_keys(self):
        _, panels = comic.parse_script_text("## 01\n画面: 1girl\n气泡: 一\n")
        self.assertEqual(panels[0]["speaker"], "")
        self.assertEqual(panels[0]["speaker2"], "")


class SpeakerTest(unittest.TestCase):
    """说话人：气泡行前缀 `（女）` / `（我）`，剥出来只决定尾巴指向谁。

    前缀**绝不能留在台词里**（会被 qwen 当文字画上去），也**绝不能误剥**——
    `（笑）你说什么` 这种括号开头不是说话人。
    """

    def test_bracket_prefix_stripped(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （女）别跟着我……\n")
        self.assertEqual(panels[0]["speaker"], "女")
        self.assertEqual(panels[0]["bubble"], "别跟着我……")

    def test_wo_means_camera_person(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （我）为什么……\n")
        self.assertEqual(panels[0]["speaker"], "我")
        self.assertEqual(panels[0]["bubble"], "为什么……")

    def test_colon_form(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 我：为什么……\n")
        self.assertEqual(panels[0]["speaker"], "我")
        self.assertEqual(panels[0]["bubble"], "为什么……")

    def test_speaker_line_applies_to_previous_bubble(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 为什么……\n说话人: 我\n")
        self.assertEqual(panels[0]["speaker"], "我")

    def test_speaker2_line_applies_to_bubble2(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: 女：一\n气泡2: 二\n说话人2: 女\n")
        self.assertEqual(panels[0]["speaker2"], "女")

    def test_non_speaker_bracket_left_alone(self):
        _, panels = comic.parse_script_text(
            "## 01\n画面: 1girl\n气泡: （笑）你说什么\n")
        self.assertEqual(panels[0]["bubble"], "（笑）你说什么")
        self.assertEqual(panels[0]["speaker"], "")


class TailDirectionTest(unittest.TestCase):
    """气泡尾巴指向谁必须写死。

    含糊的话 qwen 一律挂到画面里的女生头上——镜头外「我」说的话就变成她说的
    （2026-10-11 用户实测反馈）。
    """

    def test_girl_bubble_points_at_girl(self):
        prompt = comic.Panel("01", "1girl, solo", "别跟着我",
                             speaker="女").bubble_prompt()
        self.assertIn("尾巴指向画面中的女生", prompt)

    def test_wo_bubble_points_down_without_negative_wording(self):
        """「我」的台词：尾巴朝下 + 说明说话人在画面外，**不写否定句**。

        旧版末尾是「这个气泡不是女生说的，不要指向女生」——否定等于把「指向
        女生」这个词又喂一遍，反而更容易挂错（白熊效应，2026-10-11 用户点名）。
        """
        prompt = comic.Panel("01", "1girl, solo", "为什么",
                             speaker="我").bubble_prompt()
        self.assertIn("尾巴向下指向画面下缘", prompt)
        self.assertIn("说话人是画面外的「我」", prompt)
        self.assertNotIn("不要指向女生", prompt)
        self.assertNotIn("不是女生说的", prompt)

    def test_speaker_defaults_to_girl(self):
        self.assertEqual(comic.Panel("01", "s", "b").speaker, "女")

    def test_bubble2_uses_its_own_speaker(self):
        p = comic.Panel("01", "s", "一", speaker="我",
                        bubble2="二", speaker2="女")
        self.assertIn("向下指向画面下缘", p.bubble_prompt())
        self.assertIn("指向画面中的女生", p.bubble2_prompt())


class MultiLabelPromptTest(unittest.TestCase):
    """多条旁白必须在提示词里**逐行点名**，否则 qwen 只画一行。"""

    def test_two_lines_listed_by_index(self):
        prompt = comic.Panel("01", "s", "台词", "甲\n乙").bubble_prompt()
        self.assertIn("旁白一共 2 行", prompt)
        self.assertIn('第 1 行写 "甲"', prompt)
        self.assertIn('第 2 行写 "乙"', prompt)

    def test_single_line_uses_single_wording(self):
        prompt = comic.Panel("01", "s", "台词", "甲").bubble_prompt()
        self.assertIn("一行旁白", prompt)
        self.assertNotIn("旁白一共", prompt)

    def test_labels_helper_skips_blank(self):
        self.assertEqual(comic.Panel("01", "s", "b", "甲\n\n乙 ").labels(),
                         ["甲", "乙"])


class WriterMultiLabelValidateTest(unittest.TestCase):
    """校验：旁白至少一条、最多 MAX_LABELS 条、每条不超 MAX_LABEL 字。"""

    def test_accepts_three_labels(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 甲\n标签2: 乙\n标签3: 丙\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_rejects_too_many_labels(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 甲\n标签2: 乙\n标签3: 丙\n标签4: 丁\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("最多", why)

    def test_rejects_overlong_label_line(self):
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: %s\n" % ("长" * (comic_writer.MAX_LABEL + 1)))
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("太长", why)

    def test_rejects_missing_label(self):
        ok, why = comic_writer._validate(
            "## 01\n画面: 1girl, solo\n气泡: （女）一\n", 1)
        self.assertFalse(ok)
        self.assertIn("标签1", why)

    def test_accepts_label_at_exactly_the_cap(self):
        """上限是闭区间：正好 MAX_LABEL 字要过，多一字才挂。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: %s\n" % ("长" * comic_writer.MAX_LABEL))
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_rejects_dialogue_written_as_a_label(self):
        """`标签2: （女）…` 是台词写错了栏——必须判不合格并点名 `气泡2`。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: 她一只脚踏上车门\n标签2: （女）我不要回去！\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertFalse(ok)
        self.assertIn("（女）我不要回去！", why)
        self.assertIn("气泡2", why)

    def test_accepts_a_label_that_merely_opens_with_a_bracket(self):
        """`（她把书包扔过来）` 是正常旁白，括号里不是纯说话人词，不能误判。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: （她把书包扔过来）\n")
        ok, why = comic_writer._validate(text, 1)
        self.assertTrue(ok, why)

    def test_reports_every_dialogue_leak_at_once(self):
        """两格都写错时要在一条错因里同时点名——只报一格就只改得动一格。"""
        text = ("## 01\n画面: 1girl, solo\n气泡: （女）一\n"
                "标签1: （女）甲\n"
                "## 02\n画面: 1girl, solo\n气泡: （女）二\n"
                "标签1: 乙\n标签2: （我）丙\n")
        ok, why = comic_writer._validate(text, 2)
        self.assertFalse(ok)
        self.assertIn("格 01", why)
        self.assertIn("格 02", why)


class QwenI2iParamsTest(unittest.TestCase):
    """qwen 图生图工作流的固定参数。

    这份工作流是**共用**的：漫画「加气泡」「手动修复」「手动改字」走的都是它，
    QQ 生图的 qwen 渠道也是它——所以这里改的是全局效果，不只是漫画。
    2026-10-11 用户拍板：「我需要 qwen 图生图的步长改成 18，12 的步长效果太差了，
    必须 18 的步长」（手动改字时亲眼看出来的，12 步重画的字糊）。
    """

    def test_steps_is_18(self):
        sd = skills.load_skill(comic.BUBBLE_SKILL)
        self.assertTrue(sd, "找不到 skill %s" % comic.BUBBLE_SKILL)
        wf = skills.load_workflow(os.path.join(sd["path"], "workflow_i2i.json"))
        self.assertIsNotNone(wf, "qwen 图生图工作流读不出来")
        steps = [n["inputs"]["steps"] for n in wf.values()
                 if "steps" in (n.get("inputs") or {})]
        self.assertEqual(steps, [18], "qwen 图生图的步数必须是 18（用户 2026-10-11 拍板）")


class StudioPageTest(unittest.TestCase):
    """漫画工作台页面的接线：签名缺字段 = 界面不刷新，后端再多字段也白搭。"""

    def _page(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "web", "comic_studio.html")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_refresh_signature_includes_image_rev(self):
        """逐格 diff 的签名必须带 `p.rev`（图片文件指纹）。

        手动跑 Qwen 是**就地覆盖成品**：state 还是 done、seed 没变、final_url
        一字不差 —— 签名里不带 rev 的话这一格永远不重建，新图刷不出来
        （2026-10-11 用户报的「重跑的结果没有给到界面」）。
        """
        page = self._page()
        # 签名数组是跨行的，`.join` 收尾正好把它夹住（re.S 让 `.` 吃到换行）
        m = re.search(r"const sig = \[(.+?)\]\.join", page, re.S)
        self.assertIsNotNone(m, "找不到逐格 diff 的签名")
        self.assertIn("p.rev", m.group(1))

    def test_method_switch_is_wired_to_the_request(self):
        """画法开关必须真的把 split 发出去——只画下拉、不发参数等于摆设。

        2026-10-11 用户拍板：一遍画完做默认，但要留「分开画」当退路。前端这个
        开关若没接到 /api/comic/bubble 的 split 上，切了也不生效，而且不会有
        任何报错——典型的静默失效。
        """
        page = self._page()
        self.assertIn("meth-", page)                       # 每格一个开关
        m = re.search(r"api\('/api/comic/bubble',\s*\{[^}]*\}", page)
        self.assertIsNotNone(m, "找不到页面里调 /api/comic/bubble 的地方")
        self.assertIn("split", m.group(0))

    def test_method_switch_survives_panel_rebuild(self):
        """开关选中的值也要像手输指令一样存住。

        这一格的状态一变就整块重建 DOM；不填回来的话跑到第二遍时它会悄悄弹回
        「一遍画完」，用户以为自己在用分开画，其实不是。
        """
        page = self._page()
        self.assertIn("const _method", page)
        self.assertIn("_method[p.no]", page)


class OfficialTextRulesTest(unittest.TestCase):
    """按 Qwen 官方文本渲染规范守住的几条底线（2026-10-11 第 9 轮重整）。

    官方（Qwen-Image-Edit 官方 Space 的 Edit Instruction Rewriter）原话：文字
    内容必须用英文双引号包住；总则要求简洁、避免长句；否定内容走
    `negative_prompt` 参数、正向只写要什么。用户实测反馈：`文字（EXACT）："…"`
    里的 EXACT 被当正文，画进了成品气泡。

    这个类的意义是**反向锁死**：以后谁再往模板里加标记词、序号或否定句，这里
    直接红——因为抑制这类改动的成本，远低于再跑一轮 50 秒的图去发现它。
    """

    def _prompts(self):
        """四种形状各来一份：单气泡 / 单气泡+旁白 / 一次画两个 / 第二遍补气泡。"""
        single = comic.Panel("01", "s", "一", speaker="女")
        with_label = comic.Panel("01", "s", "一", "甲\n乙", speaker="女")
        merged = comic.Panel("01", "s", "一", "甲", "二",
                             speaker="女", speaker2="我")
        second = comic.Panel("01", "s", "一", "二",
                             speaker="女", speaker2="我")
        return {
            "单气泡": single.bubble_prompt(),
            "单气泡+旁白": with_label.bubble_prompt(),
            "一次画两个": merged.bubble_prompt(),
            "第二遍": second.bubble2_prompt(),
        }

    def test_no_marker_words(self):
        """引号就是唯一标记，没有 EXACT 之类的前缀。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("EXACT", prompt, name)

    def test_no_negative_sentences(self):
        """正文里一句否定都不许有——否定会把那个词再喂一遍给模型（白熊效应）。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("不要", prompt, name)

    def test_no_bracketed_numbering(self):
        """`【气泡1】` 这类序号是给人看的，模型会当素材。"""
        for name, prompt in self._prompts().items():
            self.assertNotIn("【", prompt, name)
            self.assertNotIn("】", prompt, name)

    def test_dialogue_is_wrapped_in_english_double_quotes(self):
        prompts = self._prompts()
        for name, prompt in prompts.items():
            self.assertIn('"', prompt, name)
        self.assertIn('"一"', prompts["单气泡"])
        self.assertIn('"甲"', prompts["单气泡+旁白"])
        self.assertIn('"二"', prompts["一次画两个"])

    def test_counts_are_positive(self):
        """条数与归属一律正面清点，不再用「不要出现第三个气泡」。"""
        p = self._prompts()
        self.assertIn("画面里的文字一共这一处", p["单气泡"])
        self.assertIn("画面里的文字一共这两处", p["单气泡+旁白"])
        self.assertIn("画面里的文字一共这三处", p["一次画两个"])
        self.assertIn("画面里的文字一共这两处", p["第二遍"])

    def test_label_style_is_stated_positively(self):
        prompt = self._prompts()["单气泡+旁白"]
        self.assertIn("纯文字加白色描边", prompt)

    def test_keep_unchanged_clause_present(self):
        """官方/社区都要求显式声明「保持不变」的那部分。"""
        for name, prompt in self._prompts().items():
            self.assertIn("保持不变", prompt, name)


if __name__ == "__main__":
    unittest.main()
