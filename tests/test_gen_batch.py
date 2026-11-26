# -*- coding: utf-8 -*-
"""工作台「直达」：原样提示词连排 N 张（2026-11-26）。

用户原话（233 粉丝群反馈）：「他在使用同一个提示词连续跑 5 张图的时候，
5 张图片都是不一样的」——因为点发送会先让大大怪优化提示词。所以要一个
**不经过模型**的入口：粘进来什么，就用什么，只换种子。

钉死四条：

  1. N 张卡各自一条任务，每张种子不一样（同种子 = 出 5 张一样的）。
  2. **一个字都不改**：不调 `_translate` / `_revise`，模型那条链一次都不进。
  3. 渠道语种这一层还要过：渠道要英文、用户写的是中文时仍然翻一遍——
     「原样」指的是不被模型改写，不是把中文硬塞给 CLIP。
  4. 访客按**真建成的张数**扣额度：只剩 2 张就建 2 张，并且明说。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.main as main                        # noqa: E402
from app.features.image import direct_gen      # noqa: E402
from app.features.image import jobs as image_jobs   # noqa: E402
from app.features.image import routes as image_routes   # noqa: E402
from app.gateways import visitor               # noqa: E402


class BatchDirectTest(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()
        # 造几条假任务，别真起 worker（_enqueue_job 会开线程跑 ComfyUI）
        self.made = []
        real = image_routes._enqueue_job

        def fake(spec, owner, user_text="", lowres_on=False):
            image_jobs.inflight_limit()      # 保留「上限 > 0」的既有判断
            if len(self.made) >= image_jobs.MAX_INFLIGHT:
                return None, "同时最多画 %d 张，等一张出来再画。" % (
                    image_jobs.MAX_INFLIGHT)
            n = len(self.made) + 1
            job = {"id": "GP-test-%03d" % n, "owner": owner,
                   "skill": spec["skill"], "prompt": spec["prompt"],
                   "seed": 1000 + n, "state": "queued", "ts": 0,
                   "ref_note": spec.get("ref_note") or ""}
            self.made.append(job)
            return job, None

        p = mock.patch.object(image_routes, "_enqueue_job", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        self._real = real
        p = mock.patch.object(image_routes, "_channel_ids",
                             return_value={"anima-2s", "moss"})
        p.start()
        self.addCleanup(p.stop)
        # 2026-11-26 起 `/api/gen/batch` 也要求身份（访客或登录），没有身份
        # 一律 401。这里默认按「登录的群友」跑，访客那两条自己改桩。
        from app.features.chat import routes as chat_routes
        p = mock.patch.object(chat_routes, "_who", return_value="10001",
                              create=True)
        p.start()
        self.addCleanup(p.stop)
        visitor.reset()
        self.addCleanup(visitor.reset)

    def _post(self, body):
        return self.client.post("/api/gen/batch", json=body)

    def test_five_jobs_each_own_seed(self):
        r = self._post({"skill": "anima-2s", "prompt": "1girl, smile",
                        "count": 5, "prompt_is_final": True})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 5)
        ids = [j["id"] for j in d["jobs"]]
        self.assertEqual(len(set(ids)), 5)          # 五条独立任务
        seeds = [j["seed"] for j in d["jobs"]]
        self.assertEqual(len(set(seeds)), 5)        # 五个不同种子
        for j in d["jobs"]:
            self.assertEqual(j["prompt"], "1girl, smile")   # 一字不改

    def test_prompt_is_not_rewritten_by_model(self):
        with mock.patch.object(direct_gen, "_translate") as tr, \
                mock.patch.object(direct_gen, "_revise") as rv:
            r = self._post({"skill": "anima-2s", "prompt": "1girl, smile",
                            "count": 3, "prompt_is_final": True})
        self.assertEqual(r.status_code, 200)
        tr.assert_not_called()
        rv.assert_not_called()

    def test_channel_language_still_checked(self):
        """中文喂给要英文的渠道：还是翻一遍，否则等于没给提示词。"""
        with mock.patch.object(image_routes, "_needs_translate",
                               return_value=True), \
                mock.patch.object(image_routes, "_translate_prompt",
                                  return_value="a girl smiling") as tp:
            r = self._post({"skill": "moss", "prompt": "一个女孩在笑",
                            "count": 2})
        self.assertEqual(r.status_code, 200)
        tp.assert_called_once()
        for j in r.get_json()["jobs"]:
            self.assertEqual(j["prompt"], "a girl smiling")

    def test_count_clamped_to_inflight_limit(self):
        r = self._post({"skill": "anima-2s", "prompt": "x", "count": 99,
                        "prompt_is_final": True})
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(r.get_json()["count"], image_jobs.MAX_INFLIGHT)

    def test_empty_prompt_rejected(self):
        r = self._post({"skill": "anima-2s", "prompt": "  ", "count": 5})
        self.assertEqual(r.status_code, 400)

    def test_unknown_skill_rejected(self):
        r = self._post({"skill": "nope", "prompt": "x", "count": 5})
        self.assertEqual(r.status_code, 400)

    def test_no_identity_is_401(self):
        """既没登录、也不是访客：直达不给排（以前这里漏了这道）。"""
        from app.features.chat import routes as chat_routes
        with mock.patch.object(chat_routes, "_who", return_value=""):
            r = self._post({"skill": "anima-2s", "prompt": "1girl",
                            "count": 5, "prompt_is_final": True})
        self.assertEqual(r.status_code, 401)

    def test_history_written_once(self):
        """同一句话连排 5 张，历史里只记一轮——记成 5 轮下一轮模型会以为
        用户连着问了五遍，改词的上下文就乱了。"""
        with mock.patch.object(image_routes, "_web_history_push") as hp, \
                mock.patch.object(image_routes, "_who", create=True):
            r = self._post({"skill": "anima-2s", "prompt": "1girl",
                            "count": 5, "prompt_is_final": True})
        self.assertEqual(r.status_code, 200)
        hp.assert_called_once()

    def test_visitor_only_pays_for_what_was_built(self):
        """访客只剩 2 张、点了 5 张：建 2 张、扣 2 张、明说一句。"""
        gid = visitor.new_id()
        gid_id = visitor.check_id(gid)
        for _ in range(3):
            visitor.consume(gid_id)              # 先用掉 3 张，剩 2
        self.assertEqual(visitor.remaining(gid_id), 2)
        with mock.patch.object(visitor, "current",
                               return_value={"id": gid_id, "raw": gid}), \
                mock.patch.object(image_routes, "_web_history_push"):
            r = self._post({"skill": "anima-2s", "prompt": "1girl",
                            "count": 5, "prompt_is_final": True})
        d = r.get_json()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(d["count"], 2)
        self.assertIn("note", d)
        self.assertEqual(visitor.remaining(gid_id), 0)

    def test_visitor_with_no_quota_is_403(self):
        gid = visitor.new_id()
        gid_id = visitor.check_id(gid)
        for _ in range(visitor.IMG_PER_DAY):
            visitor.consume(gid_id)
        with mock.patch.object(visitor, "current",
                               return_value={"id": gid_id, "raw": gid}):
            r = self._post({"skill": "anima-2s", "prompt": "1girl",
                            "count": 5, "prompt_is_final": True})
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.get_json().get("visitor"))


class NoLoginRateLimitTest(unittest.TestCase):
    """用户 2026-11-26：「什么每个 IP 每小时 10 次，没有这样的限流，取消掉。」

    登录相关的四个桶（发码 / 验码 / 游客页发码 / 游客页验码）上限填 0 =
    不设限；页面和图片那两个桶跟登录无关，原样保留。
    """

    ROUTES = (("/login/send", {"qq": "10001"}),
              ("/login/verify", {"qq": "10001", "code": "000000"}),
              ("/gv/send", {"qq": "10001"}),
              ("/gv/verify", {"qq": "10001", "code": "000000"}))

    def setUp(self):
        import friend_app
        self.app = friend_app
        self.client = friend_app.app.test_client()

    def test_login_buckets_unlimited(self):
        for name in ("send", "verify", "gv_send", "gv_verify"):
            limit, _win = self.app.LIMITS[name]
            self.assertEqual(limit, 0, name)

    def test_rate_ok_never_blocks_unlimited_bucket(self):
        """连打 500 次也不许被挡——这就是「每小时 10 次」被撤掉的证据。"""
        with self.app.app.test_request_context(
                "/", environ_base={"REMOTE_ADDR": "1.2.3.4"}):
            self.app._RL.clear()
            for _ in range(500):
                self.assertTrue(self.app.rate_ok("send"))
                self.assertTrue(self.app.rate_ok("verify"))
                self.assertTrue(self.app.rate_ok("gv_send"))
                self.assertTrue(self.app.rate_ok("gv_verify"))

    def _hit_route(self, path, body):
        """打一条登录路由，只让开页面级那道闸，返回 (状态码, 有没有在路由
        里调限流, 有没有被限流)."""
        seen = {"in_route": False, "blocked": False}
        real = self.app.rate_ok

        def watchdog(bucket, _real=real):
            # `_gate` 里那次是页面级的；再有一次就是从路由里出来的。
            if bucket != "all":
                seen["in_route"] = True
            ok = True if bucket == "all" else _real(bucket)
            if not ok and bucket != "all":
                seen["blocked"] = True
            return ok

        with mock.patch.object(self.app, "_gate", return_value=None):
            with mock.patch.object(self.app, "rate_ok", side_effect=watchdog):
                r = self.client.post(path, json=body)
        return r.status_code, seen

    def test_login_routes_have_no_rate_limit(self):
        for path, body in self.ROUTES:
            code, seen = self._hit_route(path, body)
            self.assertFalse(seen["in_route"], "%s 里还在调限流" % path)
            self.assertFalse(seen["blocked"], "%s 被限流挡了" % path)
            # 这些路由本来会因「不在名单 / 码不对」回 400/403，**不许是 429**
            self.assertNotEqual(code, 429, path)

    def test_visitor_entry_not_throttled(self):
        code, seen = self._hit_route("/login/visitor", {})
        self.assertEqual(code, 200)
        self.assertFalse(seen["in_route"], "访客入口里不该再有限流")
        self.assertFalse(seen["blocked"])


if __name__ == "__main__":
    unittest.main()
