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
                                  return_value=["大笑", "摸鱼"]):
            n = stickers.collect("qq", [("http://x/a.png", "被子教")])
        self.assertEqual(n, 1)
        index = stickers._load_index("qq")
        self.assertEqual(len(index), 1)
        rec = index[0]
        self.assertEqual(rec["tags"], ["大笑", "摸鱼"])
        self.assertEqual(rec["sender"], "被子教")
        self.assertTrue(os.path.exists(
            os.path.join(stickers._dir("qq"), rec["file"])))

    def test_duplicate_md5_is_skipped(self):
        self._setup_tmp()
        raw = self._raw_png()
        with mock.patch.object(stickers, "fetch_image", return_value=raw), \
                mock.patch.object(stickers, "_tag", return_value=[]):
            stickers.collect("qq", [("http://x/a.png", "甲")])
            n = stickers.collect("qq", [("http://x/other.png", "乙")])
        self.assertEqual(n, 0)                    # 内容一样 = 同一张图
        self.assertEqual(len(stickers._load_index("qq")), 1)

    def test_same_url_is_not_downloaded_twice(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._raw_png()) as fetch, \
                mock.patch.object(stickers, "_tag", return_value=[]):
            stickers.collect("qq", [("http://x/a.png", "甲")])
            stickers.collect("qq", [("http://x/a.png", "甲")])
        self.assertEqual(fetch.call_count, 1)     # 第二次直接跳过不下载

    def test_oversized_image_is_rejected_as_photo(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._raw_png(2000, 1500)), \
                mock.patch.object(stickers, "_tag", return_value=[]) as tag:
            n = stickers.collect("qq", [("http://x/photo.jpg", "甲")])
        self.assertEqual(n, 0)                    # 照片不入库
        tag.assert_not_called()

    def test_gif_always_accepted(self):
        self._setup_tmp()
        with mock.patch.object(stickers, "fetch_image",
                               return_value=self._gif()), \
                mock.patch.object(stickers, "_tag", return_value=["裂开"]):
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
        self.assertEqual(stickers._load_index("qq")[0]["tags"], [])


class PickTest(_TmpAgentMixin, unittest.TestCase):
    """挑选：标签互含命中、同分随机、随便=随机、无匹配=None。"""

    def _seed(self, entries):
        self._setup_tmp()
        os.makedirs(stickers._dir("qq"), exist_ok=True)
        with open(stickers._index_path("qq"), "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
                with open(os.path.join(
                        stickers._dir("qq"), e["file"]), "wb") as g:
                    g.write(b"x")

    def test_tag_match_picks_best_score(self):
        self._seed([
            {"md5": "1", "file": "a.png", "tags": ["无语", "汗"]},
            {"md5": "2", "file": "b.png", "tags": ["大笑", "好耶"]},
        ])
        rec = stickers.pick("qq", "无语")
        self.assertEqual(rec["file"], "a.png")

    def test_query_inside_tag_also_matches(self):
        self._seed([{"md5": "1", "file": "a.png", "tags": ["无语子"]}])
        self.assertEqual(stickers.pick("qq", "无语")["file"], "a.png")

    def test_random_query_picks_anything(self):
        self._seed([{"md5": "1", "file": "a.png", "tags": ["无语"]}])
        self.assertEqual(stickers.pick("qq", "随便")["file"], "a.png")

    def test_no_match_falls_back_to_random(self):
        # 甩表情不是精准检索——标签对不上就随机兜底一张，真人也经常乱甩
        self._seed([{"md5": "1", "file": "a.png", "tags": ["无语"]}])
        self.assertEqual(stickers.pick("qq", "点赞")["file"], "a.png")

    def test_missing_file_is_skipped(self):
        self._seed([{"md5": "1", "file": "a.png", "tags": ["无语"]}])
        os.remove(os.path.join(stickers._dir("qq"), "a.png"))
        self.assertIsNone(stickers.pick("qq", "无语"))

    def test_empty_library_returns_none(self):
        self._setup_tmp()
        self.assertIsNone(stickers.pick("qq", "大笑"))

    def test_collect_stops_at_limit(self):
        # 库存上限 100：满了就停收（不淘汰——哪张该删没有判断依据）
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
                mock.patch.object(stickers, "_tag", return_value=[]):
            n = stickers.collect("qq", [("http://x/new.png", "甲")])
        self.assertEqual(n, 0)
        self.assertEqual(len(stickers._load_index("qq")),
                         stickers.STICKER_LIMIT)


if __name__ == "__main__":
    unittest.main()
