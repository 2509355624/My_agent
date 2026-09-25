"""collect_sticker 工具与 stickers.ingest / 最近图片缓冲的测试。

ingest 是 collect 拆出来的单张入口，成败都要给一句能转述的话；工具层只做
选图（最近第几张）和文案映射，不碰下载。全 mock，不落真实库。
"""

import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from app import stickers                      # noqa: E402
from app.tools.normal import collect_sticker  # noqa: E402


def _raw(size=100):
    return b"\x89PNG\r\n\x1a\n" + b"x" * size


class _IndexBase(unittest.TestCase):
    """index.jsonl 指到临时文件，别碰真实库存。"""

    def setUp(self):
        p = mock.patch.object(stickers, "_index_path",
                              return_value=self._path())
        p.start()
        self.addCleanup(p.stop)
        self._clean()

    def tearDown(self):
        self._clean()

    def _path(self):
        import tempfile, os
        if not hasattr(self, "_tmp"):
            self._tmp = tempfile.NamedTemporaryFile(
                suffix=".jsonl", delete=False)
            self._tmp.close()
        return self._tmp.name

    def _clean(self):
        import tempfile, os
        import shutil
        d = stickers._dir("qq")
        shutil.rmtree(d, ignore_errors=True)
        if hasattr(self, "_tmp") and os.path.exists(self._tmp.name):
            os.remove(self._tmp.name)
            del self._tmp


class IngestTest(_IndexBase):
    """单张入库：四种结局都有明确的 (status, 编号, 短句)。"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(stickers, "_is_sticker",
                              return_value=(True, 100, 100))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(stickers, "_tag",
                              return_value=("兜帽金发兽耳", ["开心"]))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(stickers, "sniff_mime",
                              return_value="image/png")
        p.start()
        self.addCleanup(p.stop)

    def test_ok_returns_number_and_desc(self):
        with mock.patch.object(stickers, "fetch_image", return_value=_raw()):
            status, num, msg = stickers.ingest("qq", "http://a/1.png", "233")
        self.assertEqual(status, "ok")
        self.assertEqual(num, 1)                     # 空库第一张 = 1 号
        self.assertIn("兜帽金发兽耳", msg)
        with mock.patch.object(stickers, "fetch_image",
                               return_value=_raw(200)):
            status, num, _ = stickers.ingest("qq", "http://a/2.png", "")
        self.assertEqual(num, 2)                     # 编号 = 行号，只增

    def test_same_url_is_dup_without_download(self):
        with mock.patch.object(stickers, "fetch_image", return_value=_raw()):
            stickers.ingest("qq", "http://a/1.png", "")
        with mock.patch.object(stickers, "fetch_image") as fetch:
            status, num, msg = stickers.ingest("qq", "http://a/1.png", "")
        self.assertEqual(status, "dup")
        self.assertIsNone(num)
        fetch.assert_not_called()                    # 快速路径不下载

    def test_cap_blocked_with_message(self):
        with mock.patch.object(stickers, "STICKER_LIMIT", 1):
            with mock.patch.object(stickers, "fetch_image",
                                   return_value=_raw()):
                stickers.ingest("qq", "http://a/1.png", "")
            # 第二张内容必须不同——同内容不同 URL 会在 md5 去重先撞上
            with mock.patch.object(stickers, "fetch_image",
                                   return_value=_raw(200)):
                status, num, msg = stickers.ingest("qq", "http://a/2.png", "")
        self.assertEqual(status, "cap")
        self.assertIsNone(num)
        self.assertIn("库存满", msg)

    def test_download_fail_says_so(self):
        with mock.patch.object(stickers, "fetch_image",
                               side_effect=OSError("404")):
            status, num, msg = stickers.ingest("qq", "http://a/dead.png", "")
        self.assertEqual(status, "fail")
        self.assertIn("下载失败", msg)

    def test_oversized_image_rejected(self):
        with mock.patch.object(stickers, "_is_sticker",
                               return_value=(False, 4000, 3000)):
            with mock.patch.object(stickers, "fetch_image", return_value=_raw()):
                status, _, msg = stickers.ingest("qq", "http://a/big.png", "")
        self.assertEqual(status, "fail")
        self.assertIn("尺寸", msg)


class RecentImagesTest(unittest.TestCase):
    """最近图片缓冲：按会话隔离，最新在尾，只留最近几张。"""

    def setUp(self):
        stickers._RECENT_IMAGES.clear()

    def tearDown(self):
        stickers._RECENT_IMAGES.clear()

    def test_newest_last_and_per_conversation(self):
        stickers.note_image(("group", 1), "http://a/1", "甲")
        stickers.note_image(("group", 1), "http://a/2", "乙")
        stickers.note_image(("group", 2), "http://a/3", "丙")
        self.assertEqual(stickers.recent_images(("group", 1)),
                         [("http://a/1", "甲"), ("http://a/2", "乙")])
        self.assertEqual(stickers.recent_images(("group", 2)),
                         [("http://a/3", "丙")])

    def test_buffer_keeps_only_recent(self):
        for i in range(15):
            stickers.note_image(("group", 1), "http://a/%d" % i, "")
        imgs = stickers.recent_images(("group", 1))
        self.assertEqual(len(imgs), stickers._RECENT_IMAGES_MAX)
        self.assertEqual(imgs[-1][0], "http://a/14")   # 最新的还在

    def test_blank_url_ignored(self):
        stickers.note_image(("group", 1), "", "")
        self.assertEqual(stickers.recent_images(("group", 1)), [])


class CollectToolTest(unittest.TestCase):
    """工具层：选图 + 文案映射，不碰下载。"""

    def setUp(self):
        stickers._RECENT_IMAGES.clear()
        p = mock.patch.object(collect_sticker.qq_api, "current_context",
                              return_value=("group", 9))
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        stickers._RECENT_IMAGES.clear()

    def _note_two(self):
        stickers.note_image(("group", 9), "http://a/1", "甲")
        stickers.note_image(("group", 9), "http://a/2", "乙")

    def test_no_images_says_so(self):
        self.assertIn("没有图", collect_sticker.tool["function"](""))

    def test_default_takes_latest(self):
        self._note_two()
        with mock.patch.object(
                stickers, "ingest",
                return_value=("ok", 57, "已入库，编 57 号：兽耳")) as ing:
            out = collect_sticker.tool["function"]("")
        ing.assert_called_once_with("qq", "http://a/2", "乙")   # 最新那张
        self.assertIn("57", out)

    def test_which_two_takes_second_latest(self):
        self._note_two()
        with mock.patch.object(
                stickers, "ingest",
                return_value=("ok", 58, "已入库，编 58 号")) as ing:
            collect_sticker.tool["function"]("2")
        ing.assert_called_once_with("qq", "http://a/1", "甲")

    def test_which_out_of_range(self):
        self._note_two()
        out = collect_sticker.tool["function"]("5")
        self.assertIn("只有 2 张", out)

    def test_dup_message_relaid(self):
        self._note_two()
        with mock.patch.object(stickers, "ingest",
                               return_value=("dup", None, "库里已经有这张了")):
            out = collect_sticker.tool["function"]("")
        self.assertIn("已经有这张", out)

    def test_cap_message_suggests_delete(self):
        self._note_two()
        with mock.patch.object(stickers, "ingest",
                               return_value=("cap", None, "库存满了（上限 50 张）")):
            out = collect_sticker.tool["function"]("")
        self.assertIn("库存满", out)
        self.assertIn("delete_sticker", out)

    def test_fail_message_relaid(self):
        self._note_two()
        with mock.patch.object(stickers, "ingest",
                               return_value=("fail", None, "下载失败（链接可能过期了）")):
            out = collect_sticker.tool["function"]("")
        self.assertIn("下载失败", out)


class CollectStillWorksTest(_IndexBase):
    """collect 批量路径接在 ingest 上之后，行为不能变。"""

    def test_collect_counts_saved(self):
        p = mock.patch.object(stickers, "_is_sticker",
                              return_value=(True, 100, 100))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(stickers, "_tag", return_value=("", []))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(stickers, "sniff_mime",
                              return_value="image/png")
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(stickers, "fetch_image", return_value=_raw()):
            saved = stickers.collect("qq", [("http://a/1.png", "甲"),
                                            ("http://a/1.png", "甲"),   # 重复
                                            ("", "")])                  # 空地址
        self.assertEqual(saved, 1)
        # 重复那张走的是 URL 快速路径，没下载第二次
        with mock.patch.object(stickers, "fetch_image") as fetch:
            stickers.collect("qq", [("http://a/1.png", "甲")])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
