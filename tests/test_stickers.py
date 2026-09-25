"""表情包收藏库测试。

原则：零网络、零真实 agents 目录。下载和识图一律 mock；落盘走 tmp 目录。
"""

import json
import os
import unittest
from unittest import mock

from app import agents as agent_store
from app import stickers


class _TmpAgentMixin:
    """把 AGENTS_DIR 指到临时目录，并建出 agent 子目录。"""

    def _setup_tmp(self):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.join(tmp.name, "agents")
        os.makedirs(os.path.join(self.root, "qq"), exist_ok=True)
        p = mock.patch.object(agent_store, "AGENTS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)


class CollectTest(_TmpAgentMixin, unittest.TestCase):
    """收藏主流程：下载 → 去重 → 尺寸判定 → 落盘 → 打标签 → 记索引。"""

    def _raw_png(self, w=300, h=300):
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (w, h), (200, 100, 100)).save(buf, format="PNG")
        return buf.getvalue()

    def _gif(self):
        return b"GIF89a" + b"\x00" * 32

    def test_collect_saves_and_indexes(self):
        self._setup_tmp()
        raw = self._raw_png()
        with mock.patch.object(stickers, "fetch_image", return_value=raw), \
                mock.patch.object(stickers, "_tag",
                                  return_value=("猫瘫在桌上打滚",
                                                ["大笑", "摸鱼"])):
            n = stickers.collect("qq", [("http://x/a.png", "被子教")])
        self.assertEqual(n, 1)
        index = stickers._load_index("qq")
        self.assertEqual(len(index), 1)
        rec = index[0]
        self.assertEqual(rec["desc"], "猫瘫在桌上打滚")
        self.assertEqual(rec["tags"], ["大笑", "摸鱼"])
        self.assertEqual(rec["sender"], "被子教")
        self.assertTrue(os.path.exists(
            os.path.join(stickers._dir("qq"), rec["file"])))

    def test_duplicate_md5_is_skipped(self):
        self._setup_tmp()
        raw = self._raw_png()
        with mock.patch.object(stickers, "fetch_image", return_value=raw), \
                mock.patch.object(stickers, "_tag", return_value=("", [])):
            stickers.collect("qq", [("http://x/a.png", "甲")])
            n = stickers.collect("qq", [("http://x/other.png", "乙")])
        self.assertEqual(n, 0)                    # 内容一样 = 同一张图
        self.assertEqual(len(stickers._load_index("qq")), 1)

    def test_same_url_is_not_downloaded_twice(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._raw_png()) as fetch, \
                mock.patch.object(stickers, "_tag", return_value=("", [])):
            stickers.collect("qq", [("http://x/a.png", "甲")])
            stickers.collect("qq", [("http://x/a.png", "甲")])
        self.assertEqual(fetch.call_count, 1)     # 第二次直接跳过不下载

    def test_oversized_image_is_rejected_as_photo(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._raw_png(2000, 1500)), \
                mock.patch.object(stickers, "_tag",
                                  return_value=("", [])) as tag:
            n = stickers.collect("qq", [("http://x/photo.jpg", "甲")])
        self.assertEqual(n, 0)                    # 照片不入库
        tag.assert_not_called()

    def test_gif_always_accepted(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._gif()), \
                mock.patch.object(stickers, "_tag",
                                  return_value=("裂开的猫", ["裂开"])):
            n = stickers.collect("qq", [("http://x/m.gif", "甲")])
        self.assertEqual(n, 1)

    def test_download_failure_is_skipped_not_fatal(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               side_effect=RuntimeError("404")):
            n = stickers.collect("qq", [("http://x/dead.png", "甲")])
        self.assertEqual(n, 0)
        self.assertEqual(stickers._load_index("qq"), [])

    def test_tag_failure_still_saves(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._raw_png()), \
                mock.patch.object(stickers, "_tag",
                                  side_effect=RuntimeError("识图挂了")):
            n = stickers.collect("qq", [("http://x/a.png", "甲")])
        self.assertEqual(n, 1)                    # 标签失败不拦收藏
        rec = stickers._load_index("qq")[0]
        self.assertEqual(rec["desc"], "")
        self.assertEqual(rec["tags"], [])


class _SeedMixin(_TmpAgentMixin):
    """直接写索引文件当库存（文件本体用假字节占位，存在性判定够用）。"""

    def _seed(self, entries):
        self._setup_tmp()
        os.makedirs(stickers._dir("qq"), exist_ok=True)
        with open(stickers._index_path("qq"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                with open(os.path.join(
                        stickers._dir("qq"), e["file"]), "wb") as g:
                    g.write(b"x")


class CatalogTest(_SeedMixin, unittest.TestCase):
    """清单渲染：编号稳定、desc+标签、缺 desc 退回标签、缺文件不进清单。"""

    def test_catalog_lists_desc_and_tags(self):
        self._seed([
            {"md5": "1", "file": "a.png", "desc": "猫瘫在桌上打滚",
             "tags": ["慵懒", "摆烂", "摸鱼", "丧"]},
        ])
        text = stickers.catalog("qq")
        self.assertIn("[表情包库]", text)
        self.assertIn("1. 猫瘫在桌上打滚（慵懒/摆烂/摸鱼）", text)

    def test_catalog_without_desc_falls_back_to_tags(self):
        self._seed([{"md5": "1", "file": "a.png", "tags": ["无语", "汗"]}])
        self.assertIn("1. 无语、汗", stickers.catalog("qq"))

    def test_catalog_without_anything(self):
        self._seed([{"md5": "1", "file": "a.png", "tags": []}])
        self.assertIn("1. （没打上标签）", stickers.catalog("qq"))

    def test_missing_file_skipped_but_numbering_stable(self):
        # 2 号文件没了：清单里只剩 1 号和 3 号，编号不许错位——
        # 模型报 3 号必须还是原来那张
        self._seed([
            {"md5": "1", "file": "a.png", "desc": "第一张", "tags": []},
            {"md5": "2", "file": "b.png", "desc": "第二张", "tags": []},
            {"md5": "3", "file": "c.png", "desc": "第三张", "tags": []},
        ])
        os.remove(os.path.join(stickers._dir("qq"), "b.png"))
        text = stickers.catalog("qq")
        self.assertIn("1. 第一张", text)
        self.assertNotIn("第二张", text)
        self.assertIn("3. 第三张", text)

    def test_empty_library_returns_empty_string(self):
        self._setup_tmp()
        self.assertEqual(stickers.catalog("qq"), "")


class ByNumbersTest(_SeedMixin, unittest.TestCase):
    """按编号取图：语义与 catalog 严格一致（index 行号）。"""

    def _entries(self):
        return [
            {"md5": "1", "file": "a.png", "desc": "第一张", "tags": []},
            {"md5": "2", "file": "b.png", "desc": "第二张", "tags": []},
        ]

    def test_single_number(self):
        self._seed(self._entries())
        picks = stickers.records_by_numbers("qq", "2")
        self.assertEqual([(n, r["file"]) for n, r in picks],
                         [(2, "b.png")])

    def test_multiple_numbers_in_order(self):
        self._seed(self._entries())
        picks = stickers.records_by_numbers("qq", "2,1")
        self.assertEqual([r["file"] for _, r in picks], ["b.png", "a.png"])

    def test_text_with_extra_words_still_parses(self):
        self._seed(self._entries())
        picks = stickers.records_by_numbers("qq", "就发1号吧")
        self.assertEqual([r["file"] for _, r in picks], ["a.png"])

    def test_out_of_range_and_garbage_are_skipped(self):
        self._seed(self._entries())
        self.assertEqual(stickers.records_by_numbers("qq", "99"), [])
        self.assertEqual(stickers.records_by_numbers("qq", "abc"), [])
        self.assertEqual(stickers.records_by_numbers("qq", ""), [])

    def test_deleted_file_is_not_returned(self):
        self._seed(self._entries())
        os.remove(os.path.join(stickers._dir("qq"), "a.png"))
        picks = stickers.records_by_numbers("qq", "1")
        self.assertEqual(picks, [])

    def test_collect_stops_at_limit(self):
        # 库存上限 50：满了就停收（不淘汰——哪张该删没有判断依据）
        self._setup_tmp()
        os.makedirs(stickers._dir("qq"), exist_ok=True)
        with open(stickers._index_path("qq"), "w", encoding="utf-8") as f:
            for i in range(stickers.STICKER_LIMIT):
                f.write(json.dumps(
                    {"md5": str(i), "file": "s%d.png" % i, "tags": []},
                    ensure_ascii=False) + "\n")
                with open(os.path.join(
                        stickers._dir("qq"), "s%d.png" % i), "wb") as g:
                    g.write(b"x")
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new("RGB", (300, 300)).save(buf, format="PNG")
        with mock.patch.object(stickers, "fetch_image",
                               return_value=buf.getvalue()), \
                mock.patch.object(stickers, "_tag", return_value=("", [])):
            n = stickers.collect("qq", [("http://x/new.png", "甲")])
        self.assertEqual(n, 0)
        self.assertEqual(len(stickers._load_index("qq")),
                         stickers.STICKER_LIMIT)


if __name__ == "__main__":
    unittest.main()
