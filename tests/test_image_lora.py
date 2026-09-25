"""generate_image 的 lora 传参与 QQ 无底模强制测试。

原则：零网络、零显卡。ComfyUI 的 HTTP 全 mock；lora 链路判定用手工构造
的小工作流（不依赖真实 workflow.json 的节点编号）。
"""

import unittest
from unittest import mock

from app import qq_api
from app import image_jobs
from app.config import QQ_AGENT_ID
from app.tools.normal import generate_image
from app.agent_prompt import _build_tool_list


def _chain_workflow():
    """checkpoint -> 三个 lora 串成一链 + 提示词节点（模仿 image_gen_v1）。"""
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
        "86": {"class_type": "LoraLoader", "inputs": {"model": ["4", 0]}},
        "87": {"class_type": "LoraLoader", "inputs": {"model": ["86", 0]}},
        "88": {"class_type": "LoraLoader", "inputs": {"model": ["87", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "__CHARACTER__"}},
    }


class ParseLorasTest(unittest.TestCase):
    """「名字:强度」串的解析：写得出来也判得出错。"""

    def test_single(self):
        self.assertEqual(generate_image._parse_loras("a.safetensors:0.8"),
                         [("a.safetensors", 0.8)])

    def test_multi_with_spaces_and_empty_parts(self):
        self.assertEqual(
            generate_image._parse_loras(" a.safetensors:0.8 , b.safetensors:1.0 , "),
            [("a.safetensors", 0.8), ("b.safetensors", 1.0)])

    def test_negative_strength_ok(self):
        self.assertEqual(generate_image._parse_loras("a.safetensors:-0.5"),
                         [("a.safetensors", -0.5)])

    def test_missing_strength_is_error(self):
        with self.assertRaises(ValueError) as ctx:
            generate_image._parse_loras("a.safetensors")
        self.assertIn("格式", str(ctx.exception))

    def test_non_numeric_strength_is_error(self):
        with self.assertRaises(ValueError) as ctx:
            generate_image._parse_loras("a.safetensors:高")
        self.assertIn("数字", str(ctx.exception))

    def test_empty_is_error(self):
        with self.assertRaises(ValueError):
            generate_image._parse_loras("  ")


class LoraChainTest(unittest.TestCase):
    """沿 model 连线找 lora 槽顺序，不认节点编号。"""

    def test_chain_follows_model_wiring(self):
        wf = _chain_workflow()
        # 故意把 id 顺序打乱（88 在前）也能按连线排出真实顺序
        self.assertEqual(generate_image._lora_chain(wf), ["86", "87", "88"])

    def test_single_slot_workflow(self):
        wf = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "7": {"class_type": "LoraLoader", "inputs": {"model": ["4", 0]}}}
        self.assertEqual(generate_image._lora_chain(wf), ["7"])

    def test_model_only_loader_counts_as_slot(self):
        # krea2 用的是 LoraLoaderModelOnly（没有 clip 输入），也算一个槽
        wf = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "14": {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["1", 0]}}}
        self.assertEqual(generate_image._lora_chain(wf), ["14"])

    def test_no_lora_workflow(self):
        wf = {"4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "9": {"class_type": "SaveImage", "inputs": {}}}
        self.assertEqual(generate_image._lora_chain(wf), [])

    def test_non_checkpoint_source_not_treated_as_start(self):
        # model 来自非 checkpoint 节点（如 lora 加载器之外的东西）不算槽位起点
        wf = {"4": {"class_type": "SomeOtherLoader", "inputs": {}},
              "7": {"class_type": "LoraLoader", "inputs": {"model": ["4", 0]}}}
        self.assertEqual(generate_image._lora_chain(wf), [])


class ApplyLorasTest(unittest.TestCase):
    """传了就完全接管：填槽 + 关闲槽；错误返回模型能转述的话。"""

    def _apply(self, lora_str, wf=None, available=None):
        wf = wf or _chain_workflow()
        p = mock.patch.object(generate_image, "_available_loras",
                              return_value=available)
        p.start()
        self.addCleanup(p.stop)
        err = generate_image._apply_loras(wf, lora_str)
        return err, wf

    def test_fills_slots_and_zeroes_rest(self):
        err, wf = self._apply("a.safetensors:0.8")
        self.assertIsNone(err)
        self.assertEqual(wf["86"]["inputs"]["lora_name"], "a.safetensors")
        self.assertEqual(wf["86"]["inputs"]["strength_model"], 0.8)
        self.assertEqual(wf["86"]["inputs"]["strength_clip"], 0.8)
        self.assertEqual(wf["87"]["inputs"]["strength_model"], 0.0)
        self.assertEqual(wf["88"]["inputs"]["strength_clip"], 0.0)
        # 闲槽的 lora_name 保留原值没关系，强度归零即等效关闭

    def test_full_three_slots(self):
        err, wf = self._apply("a.safetensors:0.8,b.safetensors:0.5,c.safetensors:1.0")
        self.assertIsNone(err)
        self.assertEqual(wf["86"]["inputs"]["lora_name"], "a.safetensors")
        self.assertEqual(wf["87"]["inputs"]["lora_name"], "b.safetensors")
        self.assertEqual(wf["88"]["inputs"]["lora_name"], "c.safetensors")

    def test_extra_specs_truncated_not_error(self):
        err, wf = self._apply("a.safetensors:0.8,b.safetensors:0.5,c:1,d:2",
                              wf={"4": {"class_type": "CheckpointLoaderSimple",
                                        "inputs": {}},
                                  "7": {"class_type": "LoraLoader",
                                        "inputs": {"model": ["4", 0]}}})
        self.assertIsNone(err)
        self.assertEqual(wf["7"]["inputs"]["lora_name"], "a.safetensors")

    def test_bad_format_returns_message(self):
        err, _ = self._apply("a.safetensors")
        self.assertIn("格式", err)

    def test_unknown_name_lists_available(self):
        err, _ = self._apply("nope.safetensors:0.8",
                             available=["a.safetensors", "b.safetensors"])
        self.assertIn("nope.safetensors", err)
        self.assertIn("a.safetensors", err)

    def test_unknown_list_unavailable_proceeds(self):
        # ComfyUI 没连上拿不到清单时不拦截，交给 ComfyUI 自己拒
        err, wf = self._apply("whatever.safetensors:0.8", available=None)
        self.assertIsNone(err)
        self.assertEqual(wf["86"]["inputs"]["lora_name"], "whatever.safetensors")

    def test_workflow_without_slots(self):
        err, _ = self._apply("a.safetensors:0.8",
                             wf={"4": {"class_type": "CheckpointLoaderSimple",
                                       "inputs": {}}})
        self.assertIn("没有 lora 槽", err)

    def test_model_only_slot_gets_no_clip_input(self):
        # krea2 场景：ModelOnly 槽只收 model 强度，塞 clip 会被 ComfyUI 拒
        wf = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "14": {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["1", 0],
                                "lora_name": "hina.safetensors",
                                "strength_model": 1}}}
        err, wf = self._apply("x.safetensors:0.8", wf=wf)
        self.assertIsNone(err)
        self.assertEqual(wf["14"]["inputs"]["lora_name"], "x.safetensors")
        self.assertEqual(wf["14"]["inputs"]["strength_model"], 0.8)
        self.assertNotIn("strength_clip", wf["14"]["inputs"])

    def test_model_only_idle_slot_zeroes_model_only(self):
        # 传了 1 个但工作流有 2 个 ModelOnly 槽：闲槽只归零 model 强度
        wf = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
              "14": {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["1", 0], "strength_model": 1}},
              "15": {"class_type": "LoraLoaderModelOnly",
                     "inputs": {"model": ["14", 0], "strength_model": 1}}}
        err, wf = self._apply("x.safetensors:0.8", wf=wf)
        self.assertIsNone(err)
        self.assertEqual(wf["15"]["inputs"]["strength_model"], 0.0)
        self.assertNotIn("strength_clip", wf["15"]["inputs"])


class AvailableLorasTest(unittest.TestCase):
    """/object_info 拉清单：结构按 ComfyUI 的返回，挂了返回 None。"""

    def _patch_get(self, payload=None, error=None):
        def _get(url, timeout=None):
            if error:
                raise error
            return mock.Mock(json=lambda: payload,
                             raise_for_status=lambda: None)
        p = mock.patch.object(generate_image, "requests", mock.Mock(get=_get))
        p.start()
        self.addCleanup(p.stop)

    def test_parses_combo_list(self):
        self._patch_get(payload={"LoraLoader": {"input": {"required": {
            "lora_name": [["a.safetensors", "b.safetensors"], {}]}}}})
        self.assertEqual(generate_image._available_loras(),
                         ["a.safetensors", "b.safetensors"])

    def test_error_returns_none(self):
        self._patch_get(error=OSError("boom"))
        self.assertIsNone(generate_image._available_loras())


class _GenBase(unittest.TestCase):
    """照抄 test_image_jobs 的做法：HTTP/线程/发送全 mock。"""

    def setUp(self):
        image_jobs._inflight.clear()
        self.submitted = []
        def _fake_queue(workflow):
            self.submitted.append(workflow)
            return "pid"

        repls = [
            ("load_skill", mock.Mock(return_value={
                "workflow": _chain_workflow(),
                "character": "1girl, Sumire"})),
            ("_queue_prompt", _fake_queue),
            ("_qq_gate", mock.Mock(return_value=None)),
            ("is_cancelled", mock.Mock(return_value=False)),
            ("_wait_for_completion", mock.Mock(return_value={
                "outputs": {"9": {"images": [{"filename": "a.png"}]}}})),
        ]
        for target, repl in repls:
            p = mock.patch.object(generate_image, target, repl)
            p.start()
            self.addCleanup(p.stop)

        p = mock.patch.object(image_jobs, "threading",
                              mock.Mock(Thread=_SyncThread))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_image")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(image_jobs, "_send_text")
        p.start()
        self.addCleanup(p.stop)

    def _call(self, ctx, **kwargs):
        from app import image_jobs
        with mock.patch.object(qq_api, "current_context", return_value=ctx):
            with mock.patch.object(image_jobs, "wait_done",
                                   return_value={"outputs": {}}):
                return generate_image.tool["function"]("a cat", **kwargs)


class _SyncThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        self.args = args

    def start(self):
        if self.target:
            self.target(*self.args)


class QQForceNoCharacterTest(_GenBase):
    """QQ 会话里 use_character 无论传什么都不生效；网页端照常。"""

    def test_qq_forces_character_off_even_if_true(self):
        self._call(("group", "9"), use_character=True)
        text = __import__("json").dumps(self.submitted[0])
        self.assertNotIn("Sumire", text)

    def test_web_keeps_character_when_true(self):
        self._call((None, None), use_character=True)
        text = __import__("json").dumps(self.submitted[0])
        self.assertIn("Sumire", text)

    def test_lora_param_reaches_workflow(self):
        p = mock.patch.object(generate_image, "_available_loras",
                              return_value=["x.safetensors"])
        p.start()
        self.addCleanup(p.stop)
        self._call(("group", "9"), lora="x.safetensors:0.9")
        self.assertEqual(self.submitted[0]["86"]["inputs"]["lora_name"],
                         "x.safetensors")

    def test_bad_lora_returns_error_without_submitting(self):
        out = self._call(("group", "9"), lora="没强度")
        self.assertIn("格式", out)
        self.assertEqual(self.submitted, [])


class ToolDescriptionTest(unittest.TestCase):
    """QQ 看到的工具描述/参数和网页端不一样。"""

    def _block(self, agent_id):
        with mock.patch("app.agent_prompt.agent_store.allows_tool",
                        return_value=True):
            return _build_tool_list(agent_id)

    def test_qq_hides_character_param_and_mode(self):
        block = self._block(QQ_AGENT_ID)
        self.assertNotIn("use_character", block)
        self.assertNotIn("底模两种模式", block)

    def test_web_sees_character_param_and_mode(self):
        block = self._block("main")
        self.assertIn("use_character", block)
        self.assertIn("底模两种模式", block)

    def test_both_see_lora_doc(self):
        self.assertIn("lora", self._block(QQ_AGENT_ID))
        self.assertIn("lora", self._block("main"))


if __name__ == "__main__":
    unittest.main()
