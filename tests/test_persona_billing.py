# -*- coding: utf-8 -*-
"""角色扮演「代充」计费（2026-11-10）。

钉住四件会真金白银出错的事：

1. **时段**：官方口径是北京时间周一至周五、法定节假日除外，
   9:00-12:00 / 14:00-18:00 高峰；周末 / 节假日全天闲时。闲时价是高峰的一半。
2. **只扣该扣的**：免费中转站和玩家自带 Key 记 token 不记钱；只有打到
   站主付费的官方 DeepSeek 才扣余额，且余额用完就不再把付费段插进链里。
3. **链路顺序**：中转 → 付费 → 自带；没登记 / 余额 0 / 后台线程都不插付费段。
4. **只给本人看**：玩家接口和 CSV 都按请求身份过滤，游客拿全零空账本。

所有余额、流水都落在临时目录，不碰真实 state/ 和 usage/。
"""
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import flask

from app import config, llm, persona_admin_api, persona_api
from app import persona_billing as bill

TZ = ZoneInfo("Asia/Shanghai")
QQ = "2509355624"
OTHER = "123456789"


def ts(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=TZ).timestamp()


class BillBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state", "balance.json")
        self.dir = os.path.join(self.tmp.name, "usage")
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        os.makedirs(self.dir, exist_ok=True)
        for attr, val in (("STATE_PATH", self.state), ("BILL_DIR", self.dir)):
            p = mock.patch.object(bill, attr, val)
            p.start()
            self.addCleanup(p.stop)
        bill.end()
        self.addCleanup(bill.end)
        llm.set_custom_override(None)
        self.addCleanup(llm.set_custom_override, None)
        llm._DEAD.clear()                          # noqa: SLF001
        self.addCleanup(llm._DEAD.clear)           # noqa: SLF001

    def fund(self, qq=QQ, amount=5):
        acc, err = bill.recharge(qq, amount, "测试代充")
        self.assertEqual(err, "")
        return acc

    def flask_app(self, *blueprints):
        app = flask.Flask(__name__)
        for bp in blueprints:
            app.register_blueprint(bp)
        return app.test_client()


# ─── 官方时段 / 价目表 ───────────────────────────────────

class PriceRuleTest(BillBase):

    def test_peak_window_boundaries_are_right_open(self):
        cases = [
            ("2026-11-09 08:59", False),     # 周一，高峰前
            ("2026-11-09 09:00", True),
            ("2026-11-09 11:59", True),
            ("2026-11-09 12:00", False),     # 12 点整落回闲时
            ("2026-11-09 13:30", False),
            ("2026-11-09 14:00", True),
            ("2026-11-09 17:59", True),
            ("2026-11-09 18:00", False),
            ("2026-11-09 23:00", False),
        ]
        for text, want in cases:
            with self.subTest(text):
                self.assertEqual(bill.is_peak(ts(text)), want)

    def test_weekends_are_offpeak_all_day(self):
        for text in ("2026-11-14 10:00", "2026-11-15 15:30"):
            self.assertFalse(bill.is_peak(ts(text)), text)

    def test_statutory_holidays_are_offpeak(self):
        # 国办发明电〔2025〕7 号：这些区间里的工作日不算高峰
        for text in ("2026-01-01 10:00", "2026-02-17 10:00",
                     "2026-04-06 15:00", "2026-05-01 10:00",
                     "2026-06-19 10:00", "2026-09-25 10:00",
                     "2026-10-02 10:00"):
            with self.subTest(text):
                self.assertFalse(bill.is_peak(ts(text)))

    def test_a_plain_weekday_in_the_holiday_table_stays_peak(self):
        # 别把区间判断写宽：1/5 已经过完元旦了
        self.assertTrue(bill.is_peak(ts("2026-01-05 10:00")))

    def test_official_price_table(self):
        self.assertEqual(bill.price_for("deepseek-flash", False),
                         {"hit": 0.02, "miss": 1.0, "out": 4.0})
        self.assertEqual(bill.price_for("deepseek-flash", True),
                         {"hit": 0.04, "miss": 2.0, "out": 8.0})
        self.assertEqual(bill.price_for("deepseek-v4-pro", False),
                         {"hit": 0.15, "miss": 4.5, "out": 13.5})
        self.assertEqual(bill.price_for("deepseek-v4-pro", True),
                         {"hit": 0.30, "miss": 9.0, "out": 27.0})

    def test_cost_formula_tokens_times_price_is_micro_yuan(self):
        # 每类各 1,000,000 token：金额（微元）= 三组单价之和 × 1e6
        self.assertEqual(bill.cost_micro(1_000_000, 1_000_000, 1_000_000,
                                         bill.price_for("deepseek-flash", False)),
                         5_020_000)
        self.assertEqual(bill.cost_micro(1_000_000, 1_000_000, 1_000_000,
                                         bill.price_for("deepseek-flash", True)),
                         10_040_000)
        self.assertEqual(bill.cost_micro(1_000_000, 1_000_000, 1_000_000,
                                         bill.price_for("deepseek-v4-pro", False)),
                         18_150_000)
        self.assertEqual(bill.cost_micro(1_000_000, 1_000_000, 1_000_000,
                                         bill.price_for("deepseek-v4-pro", True)),
                         36_300_000)
        self.assertEqual(bill.fmt_yuan(5_000_000), "5.0000")


# ─── 余额闸门 ───────────────────────────────────────────

class BalanceTest(BillBase):

    def test_recharge_validates_both_qq_and_amount(self):
        for qq in ("", "abc", "12"):
            _, err = bill.recharge(qq, 5)
            self.assertTrue(err)
        for amount in (0, -3, "abc", None, 10001):
            _, err = bill.recharge(QQ, amount)
            self.assertTrue(err)

    def test_unregistered_qq_has_no_billing_context(self):
        self.assertEqual(bill.begin(QQ), "")
        self.assertFalse(bill.paid_enabled())
        self.assertIsNone(bill.record_call("deepseek", "deepseek-flash",
                                           1, 1, 1))

    def test_a_new_request_never_inherits_the_previous_qq(self):
        """线程池复用时，归属必须「先清空再判定」。

        否则钩子万一没跑到 / 账本读不了，上一个有钱人的 QQ 会留在这个线程里，
        下一个人的调用就被记到别人账上——这是最容易真金白银出错的一处。
        """
        self.fund()
        self.assertEqual(bill.begin(QQ), QQ)
        self.assertEqual(bill.begin(OTHER), "")     # 未登记的下一轮
        self.assertEqual(bill.current_qq(), "")
        self.assertFalse(bill.paid_enabled())
        bill.begin(QQ)
        with mock.patch.object(bill, "account",
                               side_effect=OSError("账本暂时读不了")), \
                mock.patch.object(bill, "log"):   # 别把预期内的告警刷进测试输出
            self.assertEqual(bill.begin(QQ), "")
        self.assertEqual(bill.current_qq(), "")

    def test_recharge_registers_account_and_balance(self):
        self.fund()
        self.assertEqual(bill.balance_of(QQ), 5_000_000)
        self.assertEqual(bill.accounts()[0]["qq"], QQ)
        self.assertEqual(bill.begin(QQ), QQ)
        self.assertTrue(bill.paid_enabled())

    def test_two_recharges_add_up_without_float_drift(self):
        self.fund(amount=5)
        self.fund(amount=0.1)
        self.assertEqual(bill.balance_of(QQ), 5_100_000)
        self.assertEqual(len(bill.accounts()[0]["recharges"]), 2)

    def test_free_relay_and_player_key_are_recorded_but_not_charged(self):
        self.fund()
        bill.begin(QQ)
        r1 = bill.record_call("cx_a1b2c3d4", "deepseek-v4-pro",
                              10000, 2000, 500, tag="web_x__rem")
        r2 = bill.record_call("custom", "grok-4.7",
                              3000, 400, 200, tag="web_x__rem")
        self.assertEqual(r1["channel"], "free")
        self.assertEqual(r2["channel"], "custom")
        self.assertEqual(r1["cost"], 0)
        self.assertEqual(r2["cost"], 0)
        self.assertEqual(bill.balance_of(QQ), 5_000_000)
        self.assertEqual(len(bill.read_calls(QQ)), 2)

    def test_paid_call_deducts_and_gate_closes_when_balance_hits_zero(self):
        self.fund(amount=0.001)                  # 1000 微元
        bill.begin(QQ)
        row = bill.record_call("deepseek", "deepseek-flash",
                               100000, 100000, 100000, kind="stream")
        self.assertEqual(row["channel"], "paid")
        self.assertGreater(row["cost"], 1000)
        self.assertEqual(row["balance_after"], 0)
        self.assertEqual(bill.balance_of(QQ), 0)
        self.assertFalse(bill.paid_enabled())

    def test_read_calls_is_scoped_and_guest_is_empty(self):
        self.fund()
        self.fund(OTHER, 5)
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 10, 10, 10)
        bill.end()
        bill.begin(OTHER)
        bill.record_call("deepseek", "deepseek-flash", 20, 20, 20)
        self.assertEqual(len(bill.read_calls(QQ)), 1)
        self.assertEqual(len(bill.read_calls(OTHER)), 1)
        self.assertEqual(bill.read_calls(""), [])   # 游客不能倒出全站流水
        self.assertEqual(len(bill.read_calls(None)), 2)


# ─── 降级链 ─────────────────────────────────────────────

class ChainTest(BillBase):

    def _candidates(self, customs=True):
        # PROVIDERS 里有自定义中转站是**线上私有数据**，测试不依赖它
        fake = {config.PERSONA_PROVIDER: {"model": config.PERSONA_MODEL},
                "deepseek": {"model": "deepseek-flash"}}
        p = mock.patch.object(llm, "PROVIDERS", fake)
        p.start()
        self.addCleanup(p.stop)
        llm.set_custom_override([
            {"base_url": "https://a.example/v1", "model": "m-a",
             "api_key": "k1"},
            {"base_url": "https://b.example/v1", "model": "m-b",
             "api_key": "k2"},
        ] if customs else [])
        return llm.candidates(config.PERSONA_PROVIDER, config.PERSONA_MODEL)

    def test_chain_order_is_relay_then_paid_then_player_models(self):
        self.fund()
        bill.begin(QQ)
        c = self._candidates()
        self.assertEqual(c[0], (config.PERSONA_PROVIDER, config.PERSONA_MODEL))
        self.assertEqual(c[1], ("deepseek", "deepseek-flash"))
        self.assertEqual([x[0] for x in c[2:]],
                         [llm._PERSONA_CUSTOM_PID] * 2)  # noqa: SLF001
        self.assertEqual([x[1] for x in c[2:]], ["0", "1"])

    def test_no_balance_keeps_the_original_chain(self):
        bill.begin(QQ)                 # 未登记 → 空上下文
        c = self._candidates()
        self.assertEqual(len(c), 3)
        self.assertNotIn(("deepseek", "deepseek-flash"), c)
        for item in llm.parse_chain(config.LLM_FALLBACK_CHAIN):
            self.assertNotIn(item, c)

    def test_background_thread_without_context_never_gets_paid_segment(self):
        self.fund()
        bill.begin(QQ)
        self.assertTrue(bill.paid_enabled())
        box = {}
        fake = {config.PERSONA_PROVIDER: {"model": config.PERSONA_MODEL},
                "deepseek": {"model": "deepseek-flash"}}
        with mock.patch.object(llm, "PROVIDERS", fake):
            def work():
                llm.set_custom_override([])
                box["c"] = llm.candidates(config.PERSONA_PROVIDER,
                                          config.PERSONA_MODEL)
            th = __import__("threading").Thread(target=work)
            th.start()
            th.join(5)
        self.assertNotIn(("deepseek", "deepseek-flash"), box["c"])

    def test_paid_success_is_not_mislabelled_as_free(self):
        llm.set_custom_override([])
        llm._note_persona_used("deepseek",              # noqa: SLF001
                               {"model": "deepseek-flash"})
        used = llm.persona_last_model()
        self.assertEqual(used["kind"], "paid")
        self.assertIn("代充", used["label"])

    def test_public_relay_success_still_labelled_free(self):
        llm.set_custom_override([])
        llm._note_persona_used(config.PERSONA_PROVIDER,  # noqa: SLF001
                               {"model": config.PERSONA_MODEL})
        self.assertEqual(llm.persona_last_model()["kind"], "public")


# ─── 流水 / 汇总 / CSV ──────────────────────────────────

class LedgerTest(BillBase):

    def test_paid_row_has_tokens_peak_price_and_balance_after(self):
        self.fund()
        bill.begin(QQ)
        row = bill.record_call("deepseek", "deepseek-flash",
                               14592, 657, 537, kind="stream",
                               tag="web_x__rem")
        self.assertEqual(row["qq"], QQ)
        self.assertEqual(row["session"], "web_x__rem")
        self.assertEqual((row["hit"], row["miss"], row["out"]),
                         (14592, 657, 537))
        self.assertIn("price", row)
        self.assertEqual(row["price"],
                         bill.price_for("deepseek-flash", row["peak"]))
        self.assertGreater(row["cost"], 0)
        self.assertEqual(row["balance_after"], 5_000_000 - row["cost"])
        on_disk = bill.read_calls(QQ)[0]
        self.assertEqual(on_disk["cost"], row["cost"])

    def test_llm_usage_hook_charges_only_the_paid_segment(self):
        """_record_usage 是同步/流式唯一采集点，两边都要接上计费。"""
        self.fund()
        bill.begin(QQ)
        fake = {"prompt_cache_hit_tokens": 10000,
                "prompt_cache_miss_tokens": 2000,
                "completion_tokens": 500}
        llm._record_usage(fake, provider=config.PERSONA_PROVIDER,  # noqa: SLF001
                          model=config.PERSONA_MODEL, kind="stream")
        llm._record_usage(fake, provider="custom", model="grok-4.7",  # noqa: SLF001
                          kind="stream")
        self.assertEqual(bill.balance_of(QQ), 5_000_000)
        llm._record_usage(fake, provider="deepseek",                  # noqa: SLF001
                          model="deepseek-flash", kind="stream")
        rows = bill.read_calls(QQ)
        self.assertEqual(len(rows), 3)
        self.assertEqual(sum(1 for r in rows if r["channel"] == "paid"), 1)
        self.assertEqual(sum(1 for r in rows if r["cost"] == 0), 2)
        self.assertLess(bill.balance_of(QQ), 5_000_000)

    def test_summary_has_totals_and_daily_bars(self):
        self.fund()
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        d = bill.summary(QQ, days=7)
        self.assertTrue(d["registered"])
        self.assertEqual(d["total"]["calls"], 1)
        self.assertEqual(d["total"]["paid_calls"], 1)
        self.assertEqual(d["total"]["hit"], 100)
        self.assertEqual(d["total"]["miss"], 50)
        self.assertEqual(d["total"]["out"], 20)
        self.assertEqual(len(d["daily"]), 7)
        self.assertEqual(len(d["recent"]), 1)

    def test_csv_has_bom_header_and_one_line_per_call(self):
        self.fund()
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 10, 2, 3)
        bill.record_call("cx_a1b2c3d4", "deepseek-v4-pro", 9, 1, 1)
        text = bill.csv_text(bill.read_calls(QQ))
        self.assertTrue(text.startswith("\ufeff"))
        rows = list(__import__("csv").reader(
            __import__("io").StringIO(text.lstrip("\ufeff"))))
        self.assertEqual(rows[0][0], "时间")
        self.assertIn("输入命中", rows[0])
        self.assertIn("金额(元)", rows[0])
        self.assertEqual(len(rows), 3)
        self.assertEqual([r[4] for r in rows[1:]].count("站主代充"), 1)
        self.assertEqual([r[4] for r in rows[1:]].count("免费中转"), 1)


# ─── HTTP 接口 ──────────────────────────────────────────

class PlayerApiTest(BillBase):

    def test_player_only_sees_own_balance_and_tokens(self):
        self.fund()
        self.fund(OTHER, 10)
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        c = self.flask_app(persona_api.bp)
        d = c.get("/api/persona/billing?qq=" + QQ).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["qq"], QQ)
        self.assertEqual(d["total"]["hit"], 100)
        self.assertNotEqual(d["balance_text"], "10.0000")

    def test_guest_gets_an_empty_ledger(self):
        self.fund()
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        c = self.flask_app(persona_api.bp)
        d = c.get("/api/persona/billing").get_json()
        self.assertTrue(d["ok"])
        self.assertFalse(d["registered"])
        self.assertEqual(d["total"]["calls"], 0)
        self.assertEqual(d["balance"], 0)

    def test_csv_endpoint_is_scoped_and_does_not_need_a_download_header(self):
        self.fund()
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        c = self.flask_app(persona_api.bp)
        r = c.get("/api/persona/billing/export.csv?qq=" + QQ)
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r.content_type)
        body = r.get_data(as_text=True)
        self.assertIn("站主代充", body)
        # 不带身份的游客导出只能拿到表头，不能把别人的账带出去
        r2 = c.get("/api/persona/billing/export.csv")
        self.assertNotIn(QQ, r2.get_data(as_text=True))

    def test_request_hook_attaches_the_login_qq_then_clears_it(self):
        self.fund()
        c = self.flask_app(persona_api.bp)
        with mock.patch.object(bill, "begin", wraps=bill.begin) as beg:
            c.get("/api/persona/billing?qq=" + QQ)
            beg.assert_called_once_with(QQ)
        self.assertEqual(bill.current_qq(), "")


class AdminApiTest(BillBase):

    def setUp(self):
        super().setUp()
        p = mock.patch.object(persona_admin_api, "_may_manage",
                              return_value=(True, ""))
        p.start()
        self.addCleanup(p.stop)

    def test_recharge_registers_and_lists_accounts(self):
        c = self.flask_app(persona_admin_api.bp)
        d = c.post("/api/persona_admin/billing/recharge",
                   json={"qq": QQ, "amount": 5, "note": "微信"}).get_json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["balance_text"], "5.0000")
        lst = c.get("/api/persona_admin/billing").get_json()
        self.assertEqual(lst["total"], 1)
        self.assertEqual(lst["accounts"][0]["qq"], QQ)
        self.assertEqual(lst["paid_model"], "deepseek/deepseek-flash")

    def test_recharge_rejects_bad_input(self):
        c = self.flask_app(persona_admin_api.bp)
        r = c.post("/api/persona_admin/billing/recharge",
                   json={"qq": "abc", "amount": 5})
        self.assertEqual(r.status_code, 400)
        r = c.post("/api/persona_admin/billing/recharge",
                   json={"qq": QQ, "amount": 0})
        self.assertEqual(r.status_code, 400)

    def test_detail_is_scoped_to_the_requested_qq(self):
        self.fund()
        self.fund(OTHER, 5)
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        c = self.flask_app(persona_admin_api.bp)
        d = c.get("/api/persona_admin/billing/detail?qq=" + QQ).get_json()
        self.assertEqual(d["total"], 1)
        self.assertTrue(all(r["qq"] == QQ for r in d["rows"]))

    def test_admin_csv_export_contains_bom(self):
        self.fund()
        bill.begin(QQ)
        bill.record_call("deepseek", "deepseek-flash", 100, 50, 20)
        c = self.flask_app(persona_admin_api.bp)
        r = c.get("/api/persona_admin/billing/export.csv?qq=" + QQ)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith("\ufeff"))

    def test_write_is_guarded_by_admin_permission(self):
        with mock.patch.object(persona_admin_api, "_may_manage",
                               return_value=(False, "没权限")):
            c = self.flask_app(persona_admin_api.bp)
            r = c.post("/api/persona_admin/billing/recharge",
                       json={"qq": QQ, "amount": 5})
            self.assertEqual(r.status_code, 403)


# ─── 前端静态体检 ───────────────────────────────────────

class PageTest(unittest.TestCase):
    def setUp(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "web", "persona.html"),
                  encoding="utf-8") as f:
            self.player = f.read()
        with open(os.path.join(root, "web", "persona_admin.html"),
                  encoding="utf-8") as f:
            self.admin = f.read()

    def test_player_view_entry_sits_in_the_right_sidebar(self):
        i_side = self.player.index('<aside class="side2" id="side2">')
        i_end = self.player.index("</aside>", i_side)
        i_btn = self.player.index('id="btnBill"')
        self.assertLess(i_side, i_btn)
        self.assertLess(i_btn, i_end)

    def test_player_panel_covers_balance_tokens_bars_and_csv(self):
        for needle in ('id="billMask"', 'id="billBal"', 'id="billHit"',
                       'id="billMiss"', 'id="billOut"', 'id="billBars"',
                       'id="billRows"', 'id="billCsv"',
                       '/api/persona/billing?days=',
                       '/api/persona/billing/export.csv',
                       "缓存命中", "缓存未命中", "不会过期"):
            self.assertIn(needle, self.player, "少了 %s" % needle)

    def test_current_model_distinguishes_the_paid_channel(self):
        self.assertIn('used.kind === "paid"', self.player)
        self.assertIn("官方 DeepSeek（代充余额）", self.player)

    def test_admin_has_recharge_registration_and_detail_table(self):
        for needle in ('id="btnBilling"', 'id="bQq"', 'id="bAmt"',
                       'id="bNote"', 'id="bAdd"', 'id="bAccounts"',
                       'id="bRows"', 'id="bCsvAll"',
                       "/api/persona_admin/billing/recharge",
                       "/api/persona_admin/billing/export.csv",
                       "余额不过期"):
            self.assertIn(needle, self.admin, "少了 %s" % needle)

    def test_export_this_person_requires_an_actual_selection(self):
        # 没选人时 billDownload("") 会变成「导出全部」，按钮却写着「这个人」
        self.assertIn("if (!BILL_CUR)", self.admin)
        self.assertIn("先点上面一行的 QQ 选一个人", self.admin)


if __name__ == "__main__":
    unittest.main()
