# -*- coding: utf-8 -*-
"""漫画台页面：窄屏格子头部折行 + 图片 URL 走内容指纹缓存（2026-11-11）。

钉死两条容易悄悄回退的事：

  1. `<img>` 的 src 不许再拼 `Date.now()`。HTTP 缓存按完整 URL 做 key，
     每帧一个新地址 = 后端 ETag / 304 和浏览器本地缓存全部失效，重绘一格
     就得把整本图重下。现在用 `imgUrl(url, rev)` 拼 `&v=<文件指纹>`：
     图没变 URL 稳定命中缓存，图被重写 rev 变、URL 跟着变自动拉新图。
  2. 窄屏（手机）的格子 head 必须能折行、耗时串可断行。原来是一行 flex
     里塞了 nowrap 长串，撑出 `.slot`（overflow:hidden）后内容直接被裁掉。

站主母本与群友派生页都要满足 —— 派生页是从母本生成出来的，改了母本忘了
重跑 `tools/comic_friend_patch.py` 时下面这些断言会第一时间报出来。
"""

import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = [os.path.join(_ROOT, "web", "comic_studio.html"),
         os.path.join(_ROOT, "web", "comic_studio_friend.html")]


class ComicPageCacheTest(unittest.TestCase):
    def _html(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_图片URL用文件指纹而不是时间戳(self):
        for p in PAGES:
            s = self._html(p)
            self.assertIn("function imgUrl(", s, p)
            self.assertIn("'&v=' + encodeURIComponent", s, p)
            self.assertNotIn("&t=' + Date.now()", s, p)
            self.assertIn("imgUrl(url, rev)", s, p)       # 漫画格两张图
            self.assertIn("imgUrl(u,", s, p)              # 放大层刷新
            self.assertIn("imgUrl(s.url, s.rev)", s, p)   # 写真卡
            self.assertIn("revs[0]", s, p)                # raw / final 各用自己的指纹
            self.assertIn("revs[1]", s, p)

    def test_窄屏格子头部折行(self):
        for p in PAGES:
            s = self._html(p)
            self.assertIn("@media (max-width: 640px)", s, p)
            self.assertIn(".slot .head { flex-wrap: wrap", s, p)
            self.assertIn("overflow-wrap: anywhere", s, p)
            self.assertIn(".slot .timing { flex: 1 1 100%", s, p)
            self.assertIn("grid-template-columns: minmax(0, 1fr)", s, p)

    def test_派生页与母本同步带上本次改动(self):
        host, friend = (self._html(p) for p in PAGES)
        for frag in ("function imgUrl(", "@media (max-width: 640px)",
                     "imgUrl(s.url, s.rev)", "flex-wrap: wrap"):
            self.assertIn(frag, friend, "派生页没重跑生成脚本：" + frag)
        self.assertEqual(host.count("imgUrl("), friend.count("imgUrl("))


if __name__ == "__main__":
    unittest.main()
