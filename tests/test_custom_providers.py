"""自定义中转站（app/provider_store.py）与它那几个接口的测试。

2026-10-22 用户要的：漫画台能自己加中转站（第一家 relaycore 的 grok-4.5），
加完存住，下次打开还在，且**只在漫画台的下拉里出现**。
"""

import json
import os
import shutil
import tempfile
import unittest

from app import config, main, provider_store as ps


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pv_")
        self.path = ps.store_path(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_is_empty_not_error(self):
        self.assertEqual(ps.load(self.path), {})

    def test_broken_json_falls_back_to_empty(self):
        # 配置坏了最多是「自定义渠道消失」，不能连累进程起不来。
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(ps.load(self.path), {})

    def test_wrong_shape_falls_back_to_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)
        self.assertEqual(ps.load(self.path), {})
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": {"x": 1}}, f)
        self.assertEqual(ps.load(self.path), {})

    def test_entries_missing_required_fields_are_dropped(self):
        # 没有 base_url / model 的渠道点了就报错，留着只会在下拉里当陷阱。
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": {
                "cx_00000001": {"base_url": "https://a/v1"},          # 缺 model
                "cx_00000002": {"model": "grok"},                      # 缺 base_url
                "cx_00000003": {"base_url": "https://a/v1", "model": "grok"},
            }}, f)
        items = ps.load(self.path)
        self.assertEqual(list(items), ["cx_00000003"])

    def test_bad_id_is_dropped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"providers": {
                "relaycore": {"base_url": "https://a/v1", "model": "grok"},
                "cx_zzz": {"base_url": "https://a/v1", "model": "grok"},
            }}, f)
        self.assertEqual(ps.load(self.path), {})

    def test_save_load_roundtrip(self):
        cfg = {"label": "RelayCore", "base_url": "https://a/v1",
               "model": "grok-4.5", "api_key": "sk-1", "custom": True}
        ps.save(self.path, {"cx_0000abcd": cfg})
        self.assertEqual(ps.load(self.path)["cx_0000abcd"]["model"], "grok-4.5")

    def test_save_leaves_no_tmp_behind(self):
        ps.save(self.path, {})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_new_id_does_not_collide(self):
        existing = {"cx_00000001": {}}
        pid = ps.new_id(existing)
        self.assertTrue(pid.startswith("cx_"))
        self.assertNotIn(pid, existing)


class ValidateTest(unittest.TestCase):
    BASE = {"label": "R", "base_url": "https://api.relaycore.cn/v1",
            "model": "grok-4.5", "api_key": "sk-x"}

    def _err(self, **kw):
        p = dict(self.BASE)
        p.update(kw)
        return ps.validate(p)[1]

    def test_ok(self):
        cfg, err = ps.validate(self.BASE)
        self.assertEqual(err, "")
        self.assertTrue(cfg["custom"])
        self.assertEqual(cfg["scope"], "comic")
        self.assertEqual(cfg["base_url"], "https://api.relaycore.cn/v1")

    def test_trailing_slash_is_stripped(self):
        cfg, _ = ps.validate(dict(self.BASE, base_url="https://a/v1///"))
        self.assertEqual(cfg["base_url"], "https://a/v1")

    def test_missing_fields(self):
        self.assertIn("API 地址", self._err(base_url=""))
        self.assertIn("模型名", self._err(model=""))
        self.assertIn("密钥", self._err(api_key=""))

    def test_url_must_be_http(self):
        self.assertIn("http", self._err(base_url="api.relaycore.cn/v1"))

    def test_effort_choices(self):
        self.assertIn("思考档位", self._err(reasoning_effort="none"))
        for v in ("", "low", "medium", "high"):
            self.assertEqual(self._err(reasoning_effort=v), "")

    def test_label_falls_back_to_model(self):
        cfg, _ = ps.validate(dict(self.BASE, label=""))
        self.assertEqual(cfg["label"], "grok-4.5")

    def test_unknown_scope_becomes_comic(self):
        # 默认最保守：宁可少出现，也不要漏到别的下拉里烧额度。
        cfg, _ = ps.validate(dict(self.BASE, scope="whatever"))
        self.assertEqual(cfg["scope"], "comic")

    def test_pid_keeps_created(self):
        cfg, _ = ps.validate(dict(self.BASE, created=123), pid="cx_00000001")
        self.assertEqual(cfg["created"], 123)


class PublicViewTest(unittest.TestCase):
    def test_key_is_masked(self):
        v = ps.public_view("cx_1", {"api_key": "sk-1234567890abcd",
                                    "label": "R", "base_url": "https://a/v1",
                                    "model": "grok"})
        self.assertNotIn("api_key", v)
        self.assertEqual(v["key_tail"], "…abcd")

    def test_short_key_is_not_exposed(self):
        v = ps.public_view("cx_1", {"api_key": "sk-1"})
        self.assertEqual(v["key_tail"], "已填")

    def test_no_key(self):
        self.assertEqual(ps.public_view("cx_1", {})["key_tail"], "未填")


class ReasoningFieldTest(unittest.TestCase):
    def test_empty_means_no_field(self):
        self.assertEqual(ps.reasoning_field({"reasoning_effort": ""}), {})
        self.assertEqual(ps.reasoning_field({}), {})

    def test_value_passes_through(self):
        self.assertEqual(ps.reasoning_field({"reasoning_effort": "high"}),
                         {"reasoning_effort": "high"})


class ConfigMergeTest(unittest.TestCase):
    """合并进 PROVIDERS：幂等、不覆盖内置、删掉之后真的消失。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvm_")
        self._path = config.CUSTOM_PROVIDERS_FILE
        self._providers = dict(config.PROVIDERS)
        config.CUSTOM_PROVIDERS_FILE = ps.store_path(self.tmp)

    def tearDown(self):
        config.CUSTOM_PROVIDERS_FILE = self._path
        config.PROVIDERS.clear()
        config.PROVIDERS.update(self._providers)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, items):
        ps.save(config.CUSTOM_PROVIDERS_FILE, items)

    def test_merge_and_idempotent(self):
        self._write({"cx_00000001": {"label": "R", "base_url": "https://a/v1",
                                     "model": "grok", "api_key": "k",
                                     "custom": True}})
        self.assertEqual(config.load_custom_providers(), 1)
        self.assertEqual(config.load_custom_providers(), 1)   # 不该叠加
        self.assertEqual(len([k for k, v in config.PROVIDERS.items()
                              if v.get("custom")]), 1)

    def test_removed_entry_disappears(self):
        self._write({"cx_00000001": {"base_url": "https://a/v1", "model": "grok",
                                     "custom": True}})
        config.load_custom_providers()
        self.assertIn("cx_00000001", config.PROVIDERS)
        self._write({})
        config.load_custom_providers()
        self.assertNotIn("cx_00000001", config.PROVIDERS)

    def test_builtin_is_never_overwritten(self):
        self._write({"deepseek": {"base_url": "https://evil/v1", "model": "x",
                                  "custom": True}})
        config.load_custom_providers()
        self.assertNotEqual(config.PROVIDERS["deepseek"]["base_url"],
                            "https://evil/v1")


class ApiTest(unittest.TestCase):
    """接口层。test 那个端点会真发请求，这里只测校验不过的分支。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pva_")
        self._main_path = main.CUSTOM_PROVIDERS_FILE
        self._cfg_path = config.CUSTOM_PROVIDERS_FILE
        self._providers = dict(config.PROVIDERS)
        main.CUSTOM_PROVIDERS_FILE = ps.store_path(self.tmp)
        config.CUSTOM_PROVIDERS_FILE = main.CUSTOM_PROVIDERS_FILE
        self.c = main.app.test_client()

    def tearDown(self):
        main.CUSTOM_PROVIDERS_FILE = self._main_path
        config.CUSTOM_PROVIDERS_FILE = self._cfg_path
        config.PROVIDERS.clear()
        config.PROVIDERS.update(self._providers)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _create(self, **kw):
        body = {"label": "RelayCore", "base_url": "https://api.relaycore.cn/v1",
                "model": "grok-4.5", "api_key": "sk-secret-1234",
                "reasoning_effort": "high"}
        body.update(kw)
        return self.c.post("/api/providers/custom", json=body)

    def test_create_then_appears_only_in_comic_scope(self):
        r = self._create()
        self.assertEqual(r.status_code, 200)
        pid = r.get_json()["provider"]["id"]

        comic = self.c.get("/api/providers?scope=comic").get_json()["providers"]
        self.assertIn(pid, [p["id"] for p in comic])

        # 不带 scope（主对话 / 管理页 / moss 台）看不到——私人额度不该露出去。
        plain = self.c.get("/api/providers").get_json()["providers"]
        self.assertNotIn(pid, [p["id"] for p in plain])

    def test_create_never_returns_the_key(self):
        r = self._create()
        text = r.get_data(as_text=True)
        self.assertNotIn("sk-secret-1234", text)
        self.assertIn("1234", text)          # 尾 4 位可以给

    def test_create_rejects_bad_payload(self):
        r = self._create(base_url="")
        self.assertEqual(r.status_code, 400)
        self.assertIn("API 地址", r.get_json()["error"])

    def test_update_keeps_key_when_blank(self):
        pid = self._create().get_json()["provider"]["id"]
        r = self.c.post("/api/providers/custom",
                        json={"id": pid, "label": "改名了",
                              "base_url": "https://api.relaycore.cn/v1",
                              "model": "grok-4.6", "api_key": ""})
        self.assertEqual(r.status_code, 200)
        saved = ps.load(main.CUSTOM_PROVIDERS_FILE)[pid]
        self.assertEqual(saved["api_key"], "sk-secret-1234")   # 没被清空
        self.assertEqual(saved["model"], "grok-4.6")
        self.assertEqual(saved["label"], "改名了")

    def test_update_unknown_id_is_404(self):
        r = self.c.post("/api/providers/custom",
                        json={"id": "cx_ffffffff", "base_url": "https://a/v1",
                              "model": "g", "api_key": "k"})
        self.assertEqual(r.status_code, 404)

    def test_delete(self):
        pid = self._create().get_json()["provider"]["id"]
        self.assertEqual(self.c.delete("/api/providers/custom/" + pid).status_code, 200)
        self.assertEqual(self.c.delete("/api/providers/custom/" + pid).status_code, 404)
        self.assertNotIn(pid, [p["id"] for p in
                               self.c.get("/api/providers?scope=comic")
                               .get_json()["providers"]])

    def test_builtin_providers_have_no_custom_flag(self):
        plain = self.c.get("/api/providers").get_json()["providers"]
        self.assertTrue(plain)
        self.assertFalse(any(p.get("custom") for p in plain))

    def test_test_endpoint_validates_before_network(self):
        r = self.c.post("/api/providers/custom/test",
                        json={"base_url": "", "model": "", "api_key": ""})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())


if __name__ == "__main__":
    unittest.main()
