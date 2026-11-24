# -*- coding: utf-8 -*-
"""moss 漫画台 API 测试（2026-10-21 加）。

钉死四件事：

  1. **任务名只认 `state/moss_comic` 的直接子目录**。`os.path.basename("..")`
     还是 `..`，直接 join 就跑出根目录了——这是这个端点唯一的攻击面。
  2. **spec 落盘 → 读回来不掉字段**，提示词跟着重算（页面靠它做预览）。
  3. **老任务兜底**：CLI 时代跑的那几篇没有 state.json，得靠出图目录把图找回来，
     否则页面打开全是空白。
  4. **提示词拼装**：`exactly N` 由脚本出、方位名对得上、气泡进提示词、
     人物外形同一页只展开一次（后面用 `the girl`）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask                                # noqa: E402

from app.features.comic import moss_api                               # noqa: E402


def _spec(layout="2x2", n=9):
    return {
        "title": "测试",
        "style": "anime style, clean line art, flat cel shading, soft light, calm mood",
        "cast": {"girl": "a girl with long black hair, a white shirt"},
        "pages": [{
            "n": n, "layout": layout, "scene": "a quiet room at night",
            "panels": [
                {"desc": "{girl} sits by the window in a medium shot at eye level",
                 "bubble": "晚安", "sfx": "", "caption": ""},
                {"desc": "{girl} pulls the curtain, a close-up of her hand",
                 "bubble": "", "sfx": "哗", "caption": ""},
                {"desc": "the room seen from the doorway, one lamp still on",
                 "bubble": "", "sfx": "", "caption": ""},
                {"desc": "a wide shot of the street outside, snow falling",
                 "bubble": "", "sfx": "", "caption": ""},
            ]}],
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="moss_api_")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.state)
        self._old_state = moss_api.STATE_ROOT
        self._old_out = moss_api.COMFYUI_OUTPUT_DIR
        moss_api.STATE_ROOT = self.state
        moss_api.COMFYUI_OUTPUT_DIR = os.path.join(self.tmp, "output")
        os.makedirs(moss_api.COMFYUI_OUTPUT_DIR)
        # 连载档案也必须关在临时目录里（2026-11-24 加续集）：`_publish_line`
        # 会在出图线程尾部写 `moss_series`，不隔离就是往真实存档里灌测试数据。
        ms = moss_api.moss_series
        self._old_series = (ms.STATE_ROOT, ms.INDEX_FILE)
        ms.STATE_ROOT = os.path.join(self.tmp, "series")
        ms.INDEX_FILE = os.path.join(ms.STATE_ROOT, "_index.json")
        os.makedirs(ms.STATE_ROOT)

        app = Flask(__name__)
        app.register_blueprint(moss_api.bp)
        self.c = app.test_client()

    def tearDown(self):
        moss_api.STATE_ROOT = self._old_state
        moss_api.COMFYUI_OUTPUT_DIR = self._old_out
        ms = moss_api.moss_series
        ms.STATE_ROOT, ms.INDEX_FILE = self._old_series
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk_task(self, name="20261021_1200_测试"):
        d = os.path.join(self.state, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(_spec(n=1), fh, ensure_ascii=False)
        return name, d


class TaskDirTest(_Base):
    """任务名解析：只认直接子目录。"""

    def test_rejects_traversal(self):
        for bad in ("..", "../..", "a/../../b", "", ".", "不存在"):
            with self.assertRaises(ValueError, msg=bad):
                moss_api._task_dir(bad)

    def test_rejects_absolute_outside(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with self.assertRaises(ValueError):
            moss_api._task_dir(outside)

    def test_accepts_direct_child(self):
        name, d = self._mk_task()
        self.assertEqual(moss_api._task_dir(name), os.path.realpath(d))

    def test_status_404_on_bad_task(self):
        r = self.c.get("/api/moss/status?task=../..")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["ok"])


class SaveTest(_Base):
    """落盘 → 读回：字段不丢、页号重编、提示词重算。"""

    def test_save_roundtrip(self):
        name, d = self._mk_task()
        r = self.c.post("/api/moss/save", json={"task": name, "spec": _spec(n=9)})
        self.assertEqual(r.status_code, 200)
        out = r.get_json()
        self.assertTrue(out["ok"])
        # 页号被重编成 1
        self.assertEqual(out["pages"][0]["n"], 1)
        self.assertEqual(out["title"], "测试")

        with open(os.path.join(d, "spec.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["pages"][0]["n"], 1)
        self.assertEqual(saved["pages"][0]["panels"][0]["bubble"], "晚安")
        self.assertTrue(os.path.isfile(os.path.join(d, "prompts.txt")))

    def test_prompt_shape(self):
        """落盘的提示词是 §15 官方 **Layout JSON**（2026-10-28 改，不是散文）。

        散文时代只有一句整页描述，格怎么分、气泡放哪全交给模型，实测气泡位置
        随机、旁白贴边生乱码；改成 JSON 才能逐个元素钉 bbox。所以这里按结构
        断言，不再找 "Top left panel:" 这种散文前缀。
        """
        name, _ = self._mk_task()
        out = self.c.post("/api/moss/save",
                          json={"task": name, "spec": _spec()}).get_json()
        p = out["pages"][0]["prompt"]
        self.assertIn("exactly four panels", p)

        layout = json.loads(p)
        self.assertEqual(layout["bbox_coord_space"], "abs_centralized")
        self.assertEqual(layout["bbox_format"], "yxyx")
        self.assertEqual(layout["summary"]["canvas"], "multi_panel")
        self.assertIn("meta", layout["summary"])

        items = layout["items"]
        chars = [x for x in items if x["label"] == "character"]
        texts = [x for x in items if x["label"] == "text"]
        self.assertEqual(len(chars), 4)          # 四格各一个画面元素
        # 第一格在上左、最后一格在下右——分格位置靠 bbox 钉，不靠散文顺序
        self.assertEqual(chars[0]["bbox"], [-768, -512, 0, 0])
        self.assertEqual(chars[-1]["bbox"], [0, 0, 768, 512])
        # 气泡 / 拟声都进了 items，且原文照录（不做「」包裹）
        self.assertEqual([x["text"] for x in texts], ["晚安", "哗"])
        self.assertTrue(all(x["bbox"] for x in texts))

        # 外形串**恰好展开两次**：一次在 summary.caption、一次在首个 item。
        # 这不是重复而是必须的——summary 那份独立展开（见 build_page_prompt
        # 里那个单独的 `seen`），否则 summary 会只剩"the girl"，整页失去人物
        # 定性；items 那份也要展开，否则第一格画出来认不出是谁。
        # 第 2 格起才用指代。
        self.assertEqual(p.count("a girl with long black hair"), 2)
        self.assertIn("the girl", p)
        # 场景串在 summary 里
        self.assertIn("a quiet room at night", layout["summary"]["caption"])

    def test_save_normalizes_whitespace(self):
        name, _ = self._mk_task()
        spec = _spec()
        spec["title"] = "   有空格  "
        spec["cast"]["girl"] = "  a girl with long black hair  "
        spec["pages"][0]["panels"][0]["bubble"] = "  晚安  "
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        self.assertEqual(out["title"], "有空格")
        self.assertEqual(out["cast"]["girl"], "a girl with long black hair")
        texts = [x["text"] for x in json.loads(out["pages"][0]["prompt"])["items"]
                 if x["label"] == "text"]
        self.assertEqual(texts, ["晚安", "哗"])

    def test_layout_mismatch_reported_not_blocked(self):
        """格数不够 → 体检里报，但保存照样成功（判据只报不拦）。"""
        name, _ = self._mk_task()
        spec = _spec()
        spec["pages"][0]["panels"] = spec["pages"][0]["panels"][:2]
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        self.assertTrue(out["ok"])
        self.assertTrue(any("要 4 格" in x for x in out["issues"]))

    def test_save_keeps_two_bubbles(self):
        """`bubbles` 是数组（2026-11-01 加），spec 清洗只按字符串收字段会吞掉它。

        踩过的坑：清洗那行写成 `for k in ("desc","bubble","sfx","caption")`，
        一页两条台词的页存盘后第二条直接消失，出图时那个气泡就没有了，
        而且不报错——最难查的一类。
        """
        name, d = self._mk_task()
        spec = _spec()
        spec["pages"][0]["panels"][0] = {
            "desc": "{girl} sits by the window in a medium shot at eye level",
            "bubbles": ["总算来了。", "……不是这班。"], "sfx": "", "caption": ""}
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        self.assertTrue(out["ok"])
        with open(os.path.join(d, "spec.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["pages"][0]["panels"][0]["bubbles"],
                         ["总算来了。", "……不是这班。"])

        texts = [x["text"] for x in json.loads(out["pages"][0]["prompt"])["items"]
                 if x["label"] == "text"]
        self.assertIn("总算来了。", texts)
        self.assertIn("……不是这班。", texts)

    def test_save_strips_blank_bubbles(self):
        name, _ = self._mk_task()
        spec = _spec()
        spec["pages"][0]["panels"][0] = {
            "desc": "{girl} sits by the window in a medium shot at eye level",
            "bubbles": ["  晚安  ", "", "   "], "sfx": "", "caption": ""}
        out = self.c.post("/api/moss/save", json={"task": name, "spec": spec}).get_json()
        texts = [x["text"] for x in json.loads(out["pages"][0]["prompt"])["items"]
                 if x["label"] == "text"]
        self.assertEqual(texts, ["晚安", "哗"])     # 第 2 格的拟声字照旧


class LegacyTest(_Base):
    """CLI 时代的老任务没有 state.json，靠出图目录兜底。"""

    def test_legacy_image_found(self):
        name, _ = self._mk_task("20261021_1200_旧任务")
        folder = os.path.join(moss_api.COMFYUI_OUTPUT_DIR, "web", "moss漫画",
                              name, "moss50")
        os.makedirs(folder)
        with open(os.path.join(folder, "MossComic_旧任务_P01_00001_.png"), "wb") as fh:
            fh.write(b"png")

        out = self.c.get("/api/moss/status?task=" + name).get_json()
        self.assertEqual(out["pages"][0]["status"], "done")
        self.assertTrue(out["pages"][0]["url"])
        self.assertEqual(out["pages"][0]["seed"], None)   # 老任务没记种子

        r = self.c.get("/api/moss/img?task=%s&n=1" % name)
        self.assertEqual(r.status_code, 200)

    def test_no_image_is_404(self):
        name, _ = self._mk_task()
        r = self.c.get("/api/moss/img?task=%s&n=1" % name)
        self.assertEqual(r.status_code, 404)


class ListTest(_Base):
    def test_empty(self):
        out = self.c.get("/api/moss/list").get_json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["items"], [])

    def test_lists_tasks_newest_first(self):
        n1, _ = self._mk_task("20261021_1100_甲")
        n2, _ = self._mk_task("20261021_1200_乙")
        # 让甲比乙新
        os.utime(os.path.join(self.state, n1, "spec.json"), (9e9, 9e9))
        out = self.c.get("/api/moss/list").get_json()
        self.assertEqual([x["task"] for x in out["items"]], [n1, n2])


class RenderRejectTest(_Base):
    def test_render_unknown_task(self):
        r = self.c.post("/api/moss/render", json={"task": "没有这个任务"})
        self.assertEqual(r.status_code, 404)

    def test_render_empty_spec(self):
        name, d = self._mk_task()
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump({"title": "空", "pages": []}, fh)
        r = self.c.post("/api/moss/render", json={"task": name})
        self.assertEqual(r.status_code, 400)


class DefaultStyleTest(_Base):
    """2026-11-22：画风由代码定档，模型每本自填的 style 不许漂。"""

    def test_two_agent_pins_default_style_into_spec_and_prompt(self):
        spec = _spec()
        spec["style"] = "模型自己写的另一句画风"
        with mock.patch.object(moss_api.moss_agent, "generate",
                               return_value=(spec, {"secs": 1, "pages": []})):
            r = self.c.post("/api/moss/two_agent",
                            json={"brief": "她回家了", "pages": 1})
        self.assertTrue(r.get_json()["ok"], r.get_json())
        d = os.path.join(self.state, r.get_json()["task"])
        with open(os.path.join(d, "spec.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["style"], moss_api.DEFAULT_STYLE)
        self.assertIn("screentone dot shading", moss_api.DEFAULT_STYLE)
        with open(os.path.join(d, "prompts.txt"), encoding="utf-8") as fh:
            self.assertIn(moss_api.DEFAULT_STYLE, fh.read())


class SkipDoneTest(_Base):
    """「全部出图」只补没画的（2026-11-24 用户要求，和漫画工作台一致）。

    重启 5174 之后队列全没、图还在，重跑不该把已经画好的页再画一遍。
    但单页「出图」（点名页号）和「换 seed」（reroll / 指了 seed）必须照旧
    重画，否则用户想改一张都改不了。
    """

    def _mk3(self):
        name = "20261124_1200_三页"
        d = os.path.join(self.state, name)
        os.makedirs(d)
        spec = _spec(n=1)
        spec["pages"] = [dict(spec["pages"][0], n=i) for i in (1, 2, 3)]
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
        # 第 1、3 页画好了，第 2 页没有
        os.makedirs(os.path.join(moss_api.COMFYUI_OUTPUT_DIR, "web", "moss漫画", name),
                    exist_ok=True)
        rel = "web/moss漫画/%s/moss50/1.png" % name
        full = os.path.join(moss_api.COMFYUI_OUTPUT_DIR, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(b"\x89PNG")
        moss_api._set_page(d, 1, status="done", file=rel, seed=11)
        moss_api._set_page(d, 3, status="done", file=rel, seed=33)
        return name, d

    def _render(self, body, nums):
        """跑一次 render，把出图线程换成假的，只记它收到哪些页号。"""
        got = []

        def fake_run(d, spec, n, *a, **kw):
            got.extend(n)
        with mock.patch.object(moss_api, "_run", side_effect=fake_run), \
                mock.patch.object(moss_api, "_RUNNING", {}), \
                mock.patch.object(moss_api, "_ABORT", {}):
            r = self.c.post("/api/moss/render", json=body)
        return r, got

    def test_all_only_draws_missing_pages(self):
        name, _ = self._mk3()
        r, got = self._render({"task": name}, None)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(got, [2])                      # 只补第 2 页
        out = r.get_json()
        self.assertEqual(out["skipped"], [1, 3])
        self.assertEqual(out["pages"], [2])
        self.assertFalse(out["force"])

    def test_named_page_is_redrawn_even_if_done(self):
        name, _ = self._mk3()
        r, got = self._render({"task": name, "pages": [1]}, None)
        self.assertEqual(got, [1])
        self.assertEqual(r.get_json()["skipped"], [])

    def test_reroll_still_redraws_everything(self):
        name, _ = self._mk3()
        r, got = self._render({"task": name, "reroll": True}, None)
        self.assertEqual(got, [1, 2, 3])
        self.assertTrue(r.get_json()["force"])

    def test_force_still_redraws_everything(self):
        name, _ = self._mk3()
        r, got = self._render({"task": name, "force": True}, None)
        self.assertEqual(got, [1, 2, 3])

    def test_nothing_left_says_so_without_queueing(self):
        name, d = self._mk3()
        moss_api._set_page(d, 2, status="done",
                           file="web/moss漫画/%s/moss50/1.png" % name, seed=22)
        r, got = self._render({"task": name}, None)
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(got, [])
        self.assertEqual(r.get_json()["pages"], [])
        self.assertIn("都画完了", r.get_json()["message"])

    def test_missing_file_counts_as_not_drawn(self):
        """state 说 done 但图被删了 → 该重画，不能空着。"""
        name, d = self._mk3()
        moss_api._set_page(d, 1, status="done",
                           file="web/moss漫画/%s/moss50/没有这个文件.png" % name)
        r, got = self._render({"task": name}, None)
        self.assertIn(1, got)


class PageLabelTest(_Base):
    """每页标识只能有一个真相：有图 = 已完成（2026-11-24）。

    用户报「第一页已经画过的显示准备中，这些标识必须解决」。根因有三层：
    ① 单页终止接口里用了未定义的 `log`，每次 500，页没停成；
    ② 终止会把已经有图的页也写成 canceled，徽标和图片打架；
    ③ pages_state 拿队列里的旧条目盖掉「已完成」。
    """

    def _mk(self):
        name, d = self._mk_task()
        os.makedirs(os.path.join(moss_api.COMFYUI_OUTPUT_DIR, "web", "moss漫画",
                                 name, "moss50"), exist_ok=True)
        rel = "web/moss漫画/%s/moss50/1.png" % name
        full = os.path.join(moss_api.COMFYUI_OUTPUT_DIR, rel.replace("/", os.sep))
        with open(full, "wb") as fh:
            fh.write(b"\x89PNG")
        return name, d, rel

    def test_done_page_with_file_reports_done_even_if_canceled(self):
        name, d, rel = self._mk()
        moss_api._set_page(d, 1, status="canceled", error="用户终止", file=rel)
        out = self.c.get("/api/moss/status?task=%s" % name).get_json()
        page = out["pages"][0]
        self.assertEqual(page["status"], "done")
        self.assertTrue(page["url"])

    def test_pages_state_says_done_not_queued_for_drawn_page(self):
        """队列里残留同页号的旧条目，也不能把已出图的页说成排队/准备中。"""
        name, d, rel = self._mk()
        moss_api._set_page(d, 1, status="done", file=rel)
        fake = {"running": None, "queued": [{"tag": "%s#1" % name, "ahead": 2}]}
        with mock.patch.object(moss_api.image_jobs, "snapshot", return_value=fake):
            out = self.c.get("/api/moss/pages_state?task=%s" % name).get_json()
        self.assertEqual(out["pages"]["1"]["phase"], "done")

    def test_abort_page_succeeds_and_keeps_image(self):
        """单页终止接口不能 500（之前 `log` 未定义直接炸）。"""
        name, d, rel = self._mk()
        moss_api._set_page(d, 1, status="done", file=rel, seed=7)
        r = self.c.post("/api/moss/abort_page", json={"task": name, "page": 1})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(r.get_json().get("keep"))
        st = moss_api._load_state(d)["pages"]["1"]
        self.assertEqual(st["file"], rel)             # 图没被抹掉
        self.assertEqual(st["status"], "done")        # 状态没被写坏

    def test_abort_page_marks_canceled_when_no_image(self):
        name, d, _rel = self._mk()
        r = self.c.post("/api/moss/abort_page", json={"task": name, "page": 1})
        self.assertEqual(r.status_code, 200)
        st = moss_api._load_state(d)["pages"]["1"]
        self.assertEqual(st["status"], "canceled")

    def test_render_busy_reports_page(self):
        name, d, _rel = self._mk()
        with mock.patch.object(moss_api, "_RUNNING", {d: {"t0": 0, "n": 3}}):
            r = self.c.post("/api/moss/render", json={"task": name})
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertTrue(body["busy"])
        self.assertEqual(body["page"], 3)


class ContinueTest(_Base):
    """续集（2026-11-24 用户：「给 moss 漫画增加续集，和漫画工作台一样」）。

    钉死五件事：
      1. 老任务（没有系列）第一次续集时**补建**一条单链，前情/场景从 spec 取；
      2. 续出来的新本**新开一个任务目录**，旧本一个字不动；
      3. 一条线连着续三次，页号是 1,2,3,4,5,6… **连续不跳号、不撞号**；
      4. 同一本重复调 `_series_for_task` 不会重复登记（多页的本最容易踩）；
      5. 没写剧情 / 任务不存在 → 400 / 404，不建空本子。
    """

    def _mk_book(self, name="20261101_1200_旧本", pages=2):
        d = os.path.join(self.state, name)
        os.makedirs(d, exist_ok=True)
        spec = {"title": "旧本", "cast": {"girl": "a girl with long hair"},
                "pages": [{"n": i + 1, "layout": "2x2", "scene": "scene %d" % (i + 1),
                           "beat": "第%d页的钩子" % (i + 1),
                           "panels": [{"desc": "panel %d" % (i + 1)}]}
                          for i in range(pages)]}
        with open(os.path.join(d, "spec.json"), "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False)
        for i in range(pages):
            moss_api._set_page(d, i + 1, status="done",
                               file="/tmp/p%d.png" % (i + 1), seed=i)
        return name, d, spec

    @staticmethod
    def _fake_spec(pages):
        return {"title": "", "style": "", "cast": {"girl": "a girl with long hair"},
                "pages": [{"n": i + 1, "layout": "2x2", "scene": "new room",
                           "beat": "新第%d页" % (i + 1),
                           "panels": [{"desc": "new %d" % (i + 1)}]}
                          for i in range(pages)]}

    def test_backfill_old_task_makes_a_chain(self):
        _name, d, spec = self._mk_book()
        sid, last, series = moss_api._series_for_task(d, spec)
        self.assertTrue(sid)
        self.assertEqual([p["n"] for p in series["pages"]], [1, 2])
        self.assertEqual(series["pages"][1]["parent"], series["pages"][0]["id"])
        self.assertEqual(last["n"], 2)
        # 前情/场景来自 spec 自己，不靠模型
        self.assertIn("第1页的钩子", series["pages"][0]["summary"])
        self.assertEqual(series["pages"][1]["scene"], "scene 2")

    def test_backfill_is_idempotent(self):
        _name, d, spec = self._mk_book()
        sid, _last, _s = moss_api._series_for_task(d, spec)
        sid2, _last2, series2 = moss_api._series_for_task(d, spec)
        self.assertEqual(sid, sid2)
        self.assertEqual(len(series2["pages"]), 2)      # 没被登记两遍

    def test_continue_makes_new_task_dir_and_keeps_old(self):
        name, d, _spec0 = self._mk_book()
        before = sorted(os.listdir(d))
        with mock.patch.object(moss_api.moss_agent, "generate",
                               return_value=(self._fake_spec(2), {"secs": 1})):
            r = self.c.post("/api/moss/continue",
                            json={"task": name, "brief": "剧情：她走了", "pages": 2})
        self.assertEqual(r.status_code, 200)
        out = r.get_json()
        self.assertNotEqual(out["task"], name)
        self.assertTrue(os.path.isdir(os.path.join(self.state, out["task"])))
        self.assertEqual(sorted(os.listdir(d)), before)   # 旧本没被改
        self.assertEqual(out["line"]["parent"], 2)        # 接在第 2 页后面
        self.assertEqual(out["line"]["next"], 3)
        self.assertEqual(out["cast"], {"girl": "a girl with long hair"})

    def test_continue_three_times_numbers_stay_consecutive(self):
        name, _d, _s = self._mk_book()
        prev, seen = name, []
        for k in range(3):
            with mock.patch.object(moss_api.moss_agent, "generate",
                                   return_value=(self._fake_spec(2), {"secs": 1})):
                r = self.c.post("/api/moss/continue",
                                json={"task": prev, "brief": "剧情：第%d次" % (k + 1),
                                      "pages": 2})
            out = r.get_json()
            _sid, _last, series = moss_api._series_for_task(
                os.path.join(self.state, out["task"]))
            prev = out["task"]
        seen = sorted(p["n"] for p in series["pages"])
        self.assertEqual(seen, list(range(1, 9)))          # 1..8，不跳不重

    def test_continue_brief_carries_prev_story(self):
        name, _d, _s = self._mk_book()
        got = {}

        def fake(ctx, pages, call, group=False):
            got["ctx"] = ctx
            return self._fake_spec(pages), {"secs": 1}

        with mock.patch.object(moss_api.moss_agent, "generate", side_effect=fake):
            self.c.post("/api/moss/continue",
                        json={"task": name, "brief": "剧情：她走了", "pages": 1})
        self.assertIn("第 01 页", got["ctx"])
        self.assertIn("不是另起一集", got["ctx"])
        self.assertIn("scene 2", got["ctx"])               # 只给上一页场景卡

    def test_continue_requires_brief(self):
        name, _d, _s = self._mk_book()
        r = self.c.post("/api/moss/continue", json={"task": name, "brief": "  "})
        self.assertEqual(r.status_code, 400)

    def test_continue_rejects_unknown_task(self):
        r = self.c.post("/api/moss/continue",
                        json={"task": "不存在的本子", "brief": "剧情"})
        self.assertEqual(r.status_code, 404)

    def test_status_reports_line_position(self):
        name, _d, _s = self._mk_book()
        moss_api._series_for_task(os.path.join(self.state, name))
        body = self.c.get("/api/moss/status?task=" + name).get_json()
        self.assertEqual(body["line"]["n"], 2)
        self.assertEqual(body["line"]["pages"], 2)

    def test_list_marks_page_number(self):
        name, _d, _s = self._mk_book()
        moss_api._series_for_task(os.path.join(self.state, name))
        items = self.c.get("/api/moss/list").get_json()["items"]
        row = [x for x in items if x["task"] == name][0]
        self.assertEqual(row["line_n"], 2)
        self.assertEqual(row["line_of"], 2)


if __name__ == "__main__":
    unittest.main()
