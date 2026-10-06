"""人工二审队列：审核 AI 拦下的图留一份，管理员点「过审」原样补发。

钉的是这几件事（2026-10-06 用户要的功能）：

- 拦下的图**留得住**：图从系统 temp 拷进 state/review/ —— 那是 24 小时一清的
  地方，二审是「回头再看」，图不能跟着没了；
- **过审发的就是原来那条**：caption 原文、qq_api.send_image 同一条路，对方
  收到的跟没被拦过一模一样；
- **过审后补记账本**：账本只在真发出去之后记，被拦的图当时没记上，不补的
  话「引用这张图问提示词」查不到；
- **重复点 / 图丢了 / 记录不在**都要给一句话，不能抛异常（这是补救通道）；
- 队列瘦身**先丢已处理的**，不能把还没人看过的待审挤掉。

每条用例都用自己的临时目录（`image_review.PATH` / `REVIEW_DIR` 是模块属性，
直接换掉即可），互不干扰、也不碰真实 state/。
"""

import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from app import image_review
from app.image_audit import Verdict


class _TmpCase(unittest.TestCase):
    """每条用例一份干净的队列目录 + 一张临时「待发」的图。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="review_test_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.src = os.path.join(self.dir, "Anima_02013_.jpg")
        with open(self.src, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0fake-jpeg-bytes")
        p = mock.patch.object(image_review, "PATH",
                              os.path.join(self.dir, "q.jsonl"))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_review, "REVIEW_DIR",
                              os.path.join(self.dir, "reviews"))
        p.start()
        self.addCleanup(p.stop)


class RecordTest(_TmpCase):
    """拦下一张图 → 落队，且图要留住。"""

    def test_record_keeps_a_copy_outside_temp(self):
        """拷进 state/review/：temp 那份 24 小时就没了，二审看的是这份。"""
        rid = image_review.record(self.src, "group", "233",
                                  verdict=Verdict(False, reason="低胸见沟",
                                                  category="clothing"))
        self.assertTrue(rid)
        row = image_review.get(rid)
        self.assertEqual(image_review.PENDING, row["state"])
        self.assertNotEqual(row["path"], self.src)
        self.assertTrue(os.path.isfile(row["path"]))
        with open(row["path"], "rb") as a, open(self.src, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_record_saves_enough_to_resend(self):
        """会话 / 编号 / caption / 提示词 / 审核理由：补发要的全在这儿。"""
        rid = image_review.record(
            self.src, "private", "2509355624", agent_id="qq",
            verdict=Verdict(False, reason="泳装", category="clothing"),
            meta={"tag": "HT-20261006-144800-001", "skill": "hd_3_clear",
                  "seed": 12345, "caption": "HT-20261006-144800-001 · "
                  "832×1216 · hd_3_clear · seed 12345",
                  "file": "Anima_02013_.png", "prompt": "1girl, beach"})
        row = image_review.get(rid)
        self.assertEqual("private", row["target"])
        self.assertEqual("2509355624", row["target_id"])
        self.assertEqual("qq", row["agent"])
        self.assertEqual("HT-20261006-144800-001", row["tag"])
        self.assertEqual("hd_3_clear", row["skill"])
        self.assertEqual(12345, row["seed"])
        self.assertIn("seed 12345", row["caption"])
        self.assertEqual("1girl, beach", row["prompt"])
        self.assertEqual("Anima_02013_.png", row["src_file"])
        self.assertEqual("泳装", row["reason"])
        self.assertEqual("clothing", row["category"])
        self.assertFalse(row["failed"])

    def test_id_never_collides_with_the_image_ledger(self):
        """RV- 前缀：撞上 HT- 会被账本正则当成图号，查到错的提示词。"""
        rid = image_review.record(self.src, "group", "233")
        self.assertTrue(rid.startswith("RV-"))
        from app import image_log
        self.assertEqual([], image_log.find_tags(rid),
                         "二审 id 不能长得像生图编号")

    def test_record_failure_never_raises(self):
        """补救通道自己炸了不能把审核闸门带下去。"""
        with mock.patch.object(image_review, "_keep_file",
                               side_effect=OSError("disk full")):
            rid = image_review.record(self.src, "group", "233")
        # 拷图失败 → 退回原路径，记录照样成立
        self.assertTrue(rid)
        self.assertEqual(self.src, image_review.get(rid)["path"])

    def test_failed_audit_is_flagged(self):
        """审核没生效（failed=True）跟真判违规分开——二审要看得出来。"""
        rid = image_review.record(self.src, "group", "233",
                                  verdict=Verdict(False, reason="识图失败",
                                                  failed=True))
        self.assertTrue(image_review.get(rid)["failed"])


class ListTest(_TmpCase):
    """列表：最新在前、能按状态筛。"""

    def _add(self, n, **kw):
        ids = []
        for i in range(n):
            ids.append(image_review.record(self.src, "group", str(i), **kw))
        return ids

    def test_newest_first(self):
        ids = self._add(3)
        self.assertEqual(list(reversed(ids)),
                         [r["id"] for r in image_review.items()])

    def test_filter_by_state(self):
        ids = self._add(3)
        with mock.patch("app.qq_api.send_image") as send:
            image_review.approve(ids[0])
        self.assertTrue(send.called)
        self.assertEqual(2, len(image_review.items(state=image_review.PENDING)))
        self.assertEqual(1, len(image_review.items(state=image_review.APPROVED)))
        self.assertEqual(3, len(image_review.items()))

    def test_pending_count(self):
        ids = self._add(2)
        self.assertEqual(2, image_review.pending_count())
        with mock.patch("app.qq_api.send_image"):
            image_review.approve(ids[0])
        self.assertEqual(1, image_review.pending_count())


class ApproveTest(_TmpCase):
    """点「过审」：原样补发 + 补记账本。"""

    def _blocked(self, **meta):
        meta.setdefault("tag", "HT-20261006-144800-001")
        meta.setdefault("skill", "hd_3_clear")
        meta.setdefault("seed", 999)
        meta.setdefault("caption", "HT-20261006-144800-001 · seed 999")
        meta.setdefault("prompt", "1girl, red dress")
        meta.setdefault("file", "Anima_02013_.png")
        return image_review.record(self.src, "group", "233", agent_id="qq",
                                   verdict=Verdict(False, reason="擦边"),
                                   meta=meta)

    def test_approve_resends_with_the_original_caption(self):
        """对方收到的跟一张没被拦过的图一个字都不差。"""
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            ok, msg = image_review.approve(rid)
        self.assertTrue(ok, msg)
        send.assert_called_once_with("group", "233",
                                     image_review.get(rid)["path"],
                                     caption="HT-20261006-144800-001 · seed 999")

    def test_approve_backfills_the_ledger(self):
        """账本只在真发出去之后记——补发完了就得补上，否则引用查不到提示词。"""
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image"), \
             mock.patch("app.image_log.save") as save:
            ok, _msg = image_review.approve(rid)
        self.assertTrue(ok)
        save.assert_called_once()
        self.assertEqual("HT-20261006-144800-001", save.call_args.args[0])
        kw = save.call_args.kwargs
        self.assertEqual("1girl, red dress", kw["prompt"])
        self.assertEqual("Anima_02013_.png", kw["file"])
        self.assertEqual("group", kw["target"])
        self.assertEqual("233", kw["target_id"])
        self.assertEqual(999, kw["seed"])

    def test_approve_twice_is_refused(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            self.assertTrue(image_review.approve(rid)[0])
            ok, msg = image_review.approve(rid)
        self.assertFalse(ok)
        self.assertIn("已经处理过", msg)
        self.assertEqual(1, send.call_count, "不该重复发第二遍")

    def test_missing_image_gives_a_message_not_a_crash(self):
        rid = self._blocked()
        row = image_review.get(rid)
        os.remove(row["path"])
        with mock.patch("app.qq_api.send_image") as send:
            ok, msg = image_review.approve(rid)
        self.assertFalse(ok)
        self.assertIn("不在了", msg)
        self.assertFalse(send.called)

    def test_unknown_id_is_safe(self):
        ok, msg = image_review.approve("RV-19990101-000000-000")
        self.assertFalse(ok)
        self.assertIn("不在了", msg)

    def test_no_recipient_is_refused(self):
        rid = image_review.record(self.src, "", "")
        with mock.patch("app.qq_api.send_image") as send:
            ok, msg = image_review.approve(rid)
        self.assertFalse(ok)
        self.assertIn("接收方", msg)
        self.assertFalse(send.called)

    def test_send_failure_keeps_it_pending(self):
        """发不出去就还是待审——管理员还能再点一次。"""
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image",
                        side_effect=RuntimeError("onebot down")):
            ok, msg = image_review.approve(rid)
        self.assertFalse(ok)
        self.assertIn("发送失败", msg)
        self.assertEqual(image_review.PENDING,
                         image_review.get(rid)["state"])


class RejectTest(_TmpCase):
    """点「确认违规」：标记拒发，图留着备查。"""

    def test_reject_marks_and_keeps_the_file(self):
        rid = image_review.record(self.src, "group", "233")
        path = image_review.get(rid)["path"]
        ok, _msg = image_review.reject(rid)
        self.assertTrue(ok)
        self.assertEqual(image_review.REJECTED,
                         image_review.get(rid)["state"])
        self.assertTrue(os.path.isfile(path), "图留着备查，不删")

    def test_reject_twice_is_refused(self):
        rid = image_review.record(self.src, "group", "233")
        self.assertTrue(image_review.reject(rid)[0])
        self.assertFalse(image_review.reject(rid)[0])

    def test_rejected_cannot_be_approved(self):
        rid = image_review.record(self.src, "group", "233")
        image_review.reject(rid)
        with mock.patch("app.qq_api.send_image") as send:
            ok, _msg = image_review.approve(rid)
        self.assertFalse(ok)
        self.assertFalse(send.called)


class SweepTest(_TmpCase):
    """瘦身：先丢已处理的，待审留着。"""

    def test_pending_survives_a_processing_burst(self):
        """随机口令重抽会连着拦好几张——待审不能被它们挤掉。"""
        ids = []
        for i in range(image_review.MAX_ITEMS + 5):
            ids.append(image_review.record(self.src, "group", str(i)))
        processed = ids[:-3]
        pending = ids[-3:]
        with mock.patch("app.qq_api.send_image"):
            for rid in processed:
                image_review.approve(rid)
        # 再塞几条触发瘦身
        for i in range(3):
            image_review.record(self.src, "group", "new%d" % i)
        left = {r["id"] for r in image_review.items()}
        for rid in pending:
            self.assertIn(rid, left, "待审被瘦身挤掉了")

    def test_expired_rows_are_dropped_with_their_files(self):
        # 先把保留期放到无限，塞一条「很久以前」的记录进去；再按正常保留期
        # 落一条新的，触发瘦身把它（和它的图）清掉。
        with mock.patch.object(image_review, "KEEP_SECONDS", 10 ** 9):
            rid = image_review.record(self.src, "group", "233",
                                      ts=time.time() - 10 ** 6)
        path = image_review.get(rid)["path"]
        self.assertTrue(os.path.exists(path))
        image_review.record(self.src, "group", "233")     # 触发瘦身
        self.assertIsNone(image_review.get(rid))
        self.assertFalse(os.path.exists(path))

    def test_sweep_never_touches_files_outside_the_review_dir(self):
        """拷图失败退回的 temp 路径不是我们的，别顺手删了。"""
        outside = os.path.join(self.dir, "outside.jpg")
        with open(outside, "wb") as f:
            f.write(b"x")
        with mock.patch.object(image_review, "_keep_file",
                               return_value=outside):
            image_review.record(self.src, "group", "233",
                                ts=time.time() - image_review.KEEP_SECONDS - 60)
            image_review.record(self.src, "group", "233")
        self.assertTrue(os.path.exists(outside), "别人的文件被删了")


class AuditHookTest(unittest.TestCase):
    """审核闸门判违规 → 自动进队（这是队列唯一的写入点）。"""

    def test_blocked_image_lands_in_the_queue(self):
        from app import image_audit
        dir_ = tempfile.mkdtemp(prefix="review_hook_")
        self.addCleanup(shutil.rmtree, dir_, True)
        with open(os.path.join(dir_, "p.jpg"), "wb") as f:
            f.write(b"x")
        path = os.path.join(dir_, "p.jpg")
        with mock.patch.object(image_review, "PATH",
                               os.path.join(dir_, "q.jsonl")), \
             mock.patch.object(image_review, "REVIEW_DIR",
                               os.path.join(dir_, "reviews")), \
             mock.patch.object(image_audit, "check",
                               return_value=Verdict(False, reason="走光",
                                                    category="clothing")), \
             mock.patch("app.agents.image_audit_enabled",
                        return_value=True), \
             mock.patch("app.qq_api.send_group") as sg:
            ok = image_audit.allow_send(path, "qq", "group", "233",
                                        meta={"tag": "HT-1", "caption": "C"})
            # ⚠️ 断言必须在 patch 生效期间做：出了 with，PATH 已经指回
            # state/ 下的真队列了，这里查的必须是临时那份。
            self.assertFalse(ok)
            sg.assert_called_once()
            rows = image_review.items(state=image_review.PENDING)
        self.assertEqual(1, len(rows))
        self.assertEqual("HT-1", rows[0]["tag"])
        self.assertEqual("走光", rows[0]["reason"])

    def test_queue_failure_does_not_change_the_verdict(self):
        """二审队列写不进去，图照旧按拦截处理。"""
        from app import image_audit
        dir_ = tempfile.mkdtemp(prefix="review_hook2_")
        self.addCleanup(shutil.rmtree, dir_, True)
        path = os.path.join(dir_, "p.jpg")
        with open(path, "wb") as f:
            f.write(b"x")
        with mock.patch.object(image_audit, "check",
                               return_value=Verdict(False, reason="x")), \
             mock.patch("app.agents.image_audit_enabled", return_value=True), \
             mock.patch("app.image_review.record",
                        side_effect=OSError("disk full")), \
             mock.patch("app.qq_api.send_group") as sg:
            ok = image_audit.allow_send(path, "qq", "group", "233")
        self.assertFalse(ok)
        sg.assert_called_once()

    def test_allowed_image_never_enters_the_queue(self):
        from app import image_audit
        with mock.patch.object(image_audit, "check",
                               return_value=Verdict(True)), \
             mock.patch("app.agents.image_audit_enabled", return_value=True), \
             mock.patch("app.image_review.record") as rec:
            self.assertTrue(image_audit.allow_send("x.jpg", "qq", "group", "233"))
        self.assertFalse(rec.called)


class RoutesTest(_TmpCase):
    """管理页那三个接口：列队列 / 过审 / 驳回 + 取图。"""

    def setUp(self):
        _TmpCase.setUp(self)
        from app import main
        self.client = main.app.test_client()

    def _blocked(self):
        return image_review.record(self.src, "group", "233",
                                   verdict=Verdict(False, reason="擦边",
                                                   category="clothing"),
                                   meta={"tag": "HT-1", "skill": "hd_3_clear",
                                         "seed": 7, "caption": "HT-1 · seed 7",
                                         "prompt": "1girl, dress"})

    def test_list_returns_items_and_pending_count(self):
        self._blocked()
        r = self.client.get("/api/agent/qq/image_review")
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(1, len(d["items"]))
        self.assertEqual(1, d["pending"])
        self.assertEqual("HT-1", d["items"][0]["tag"])

    def test_list_can_filter_out_processed_ones(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image"):
            self.client.post("/api/agent/qq/image_review/%s/approve" % rid)
        d = self.client.get("/api/agent/qq/image_review?state=pending").get_json()
        self.assertEqual(0, len(d["items"]))
        d = self.client.get("/api/agent/qq/image_review?state=approved").get_json()
        self.assertEqual(1, len(d["items"]))

    def test_approve_endpoint_resends(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            r = self.client.post("/api/agent/qq/image_review/%s/approve" % rid)
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(send.called)
        self.assertEqual(0, r.get_json()["pending"])

    def test_reject_endpoint_marks_it(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            r = self.client.post("/api/agent/qq/image_review/%s/reject" % rid)
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertFalse(send.called)
        self.assertEqual(image_review.REJECTED, image_review.get(rid)["state"])

    def test_unknown_id_is_400_not_a_crash(self):
        r = self.client.post("/api/agent/qq/image_review/RV-nope/approve")
        self.assertEqual(400, r.status_code)
        self.assertFalse(r.get_json()["ok"])

    def test_image_route_serves_the_kept_file(self):
        rid = self._blocked()
        name = os.path.basename(image_review.get(rid)["path"])
        r = self.client.get("/api/review/image/" + name)
        self.assertEqual(200, r.status_code)
        with open(self.src, "rb") as f:
            self.assertEqual(f.read(), r.data)

    def test_image_route_refuses_path_traversal(self):
        """只认 state/review 这一层的文件名，`..` 不该摸到别处。"""
        r = self.client.get("/api/review/image/..%2f..%2fq.jsonl")
        self.assertEqual(404, r.status_code)

    def test_image_route_404_on_missing(self):
        self.assertEqual(404,
                         self.client.get("/api/review/image/nope.jpg").status_code)


class GlobalRoutesTest(_TmpCase):
    """独立全局界面那三个接口：列全局队列 / 过审 / 驳回（不绑 agent）。

    与 RoutesTest 的唯一差别：URL 里没有 agent 段，且列表是**跨 agent 聚合**
    的——一个界面看全部 bot / 群 / 私聊被拦的图。
    """

    def setUp(self):
        _TmpCase.setUp(self)
        from app import main
        self.client = main.app.test_client()

    def _blocked(self, target="group", target_id="233", **kw):
        return image_review.record(self.src, target, target_id,
                                   verdict=Verdict(False, reason="擦边",
                                                   category="clothing"),
                                   meta={"tag": "HT-1", "skill": "hd_3_clear",
                                         "seed": 7, "caption": "HT-1 · seed 7",
                                         "prompt": "1girl, dress"}, **kw)

    def test_global_list_returns_all_items(self):
        self._blocked()
        r = self.client.get("/api/image_review")
        self.assertEqual(200, r.status_code)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(1, len(d["items"]))
        self.assertEqual(1, d["pending"])

    def test_global_list_aggregates_across_agents(self):
        """全局列表不分 agent：两个不同 bot 的图都该出现。"""
        self._blocked(agent_id="qq")
        self._blocked(agent_id="other_bot", target="private", target_id="999")
        d = self.client.get("/api/image_review").get_json()
        self.assertEqual(2, len(d["items"]))
        agents = {it["agent"] for it in d["items"]}
        self.assertEqual({"qq", "other_bot"}, agents)

    def test_global_list_can_filter_processed(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image"):
            self.client.post("/api/image_review/%s/approve" % rid)
        d = self.client.get("/api/image_review?state=pending").get_json()
        self.assertEqual(0, len(d["items"]))
        d = self.client.get("/api/image_review?state=approved").get_json()
        self.assertEqual(1, len(d["items"]))

    def test_global_approve_resends(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            r = self.client.post("/api/image_review/%s/approve" % rid)
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertTrue(r.get_json()["ok"])
        self.assertTrue(send.called)

    def test_global_reject_marks_it(self):
        rid = self._blocked()
        with mock.patch("app.qq_api.send_image") as send:
            r = self.client.post("/api/image_review/%s/reject" % rid)
        self.assertEqual(200, r.status_code, r.get_json())
        self.assertFalse(send.called)
        self.assertEqual(image_review.REJECTED, image_review.get(rid)["state"])

    def test_global_unknown_id_is_400_not_a_crash(self):
        r = self.client.post("/api/image_review/RV-nope/approve")
        self.assertEqual(400, r.status_code)
        self.assertFalse(r.get_json()["ok"])
