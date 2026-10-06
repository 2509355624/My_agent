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
    """checkpoint -> 三个 lora 串成一链 + 提示词节点（手工造的最小结构）。

    结构不再模仿任何**具体**渠道：`image_gen_v1` 已归档、4 个动漫渠道是
    UNETLoader + `LoraLoaderModelOnly`。这里只用 `LoraLoader`（model+clip）
    把「沿 model 连线找槽顺序」这条逻辑单独测出来，不依赖真实节点编号。
    """
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
        "86": {"class_type": "LoraLoader", "inputs": {"model": ["4", 0]}},
        "87": {"class_type": "LoraLoader", "inputs": {"model": ["86", 0]}},
        "88": {"class_type": "LoraLoader", "inputs": {"model": ["87", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "__MULTI_PROMPTS__"}},
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

    def test_gguf_unet_loader_counts_as_start(self):
        # krea2 实际工作流：GGUF 模型走 UnetLoaderGGUF，不是 CheckpointLoader——
        # 起点识别必须把它也算上，否则整条 lora 链找不到 → 误报"没有 lora 槽"
        wf = {"1": {"class_type": "UnetLoaderGGUF", "inputs": {}},
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
        image_jobs._reset()
        self.submitted = []

        def _fake_queue(workflow):
            self.submitted.append(workflow)
            return "pid"

        for target, repl in (
            ("load_skill", mock.Mock(return_value={
                "workflow": _chain_workflow()})),
            ("_qq_gate", mock.Mock(return_value=None)),
            ("is_cancelled", mock.Mock(return_value=False)),
        ):
            p = mock.patch.object(generate_image, target, repl)
            p.start()
            self.addCleanup(p.stop)

        # 提交挪进了 image_jobs 的 worker：这里拦它那边的出口，并把 worker
        # 线程挡在门外，由 _call 自己 _drain() 同步驱动。
        for target, repl in (
            ("_queue_prompt", _fake_queue),
            ("_ensure_worker", lambda: None),
            ("wait_done", mock.Mock(return_value={"outputs": {}})),
            ("_send_image", mock.Mock()),
            ("_send_text", mock.Mock()),
            # 入队前探活 / 提交前查显存水位：不挡就会真去 GET 真机的
            # /system_stats，ComfyUI 没开时这三条用例必挂（跟被测逻辑无关）。
            ("comfy_alive", lambda: True),
            ("_free_vram_gb", lambda: None),
        ):
            p = mock.patch.object(image_jobs, target, repl)
            p.start()
            self.addCleanup(p.stop)

        def _sync_wait(self, poll=2):
            """测试里没有 worker 线程，网页侧 wait 时自己把队列跑完。"""
            image_jobs._drain()
            if self.error is not None:
                raise self.error
            return self.entry

        p = mock.patch.object(image_jobs.Job, "wait", _sync_wait)
        p.start()
        self.addCleanup(p.stop)

    def _call(self, ctx, **kwargs):
        with mock.patch.object(qq_api, "current_context", return_value=ctx):
            out = generate_image.tool["function"]("a cat", **kwargs)
            image_jobs._drain()
            return out


class _SyncThread:
    def __init__(self, target=None, args=(), **kwargs):
        self.target = target
        self.args = args

    def start(self):
        if self.target:
            self.target(*self.args)


class LoraParamTest(_GenBase):
    """lora 参数沿 model 链落进工作流；写错时拒收、不提交。

    历史：这里原本是 `QQForceNoCharacterTest`，测的是「QQ 侧强制关掉
    `use_character` 角色底模」。角色底模机制随 SD 渠道在 2026-09-30 一起下线
    （全仓已无 `character.txt` / `__CHARACTER__`），那两条用例已无对象，删掉。
    """

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

    def test_neither_side_sees_the_retired_character_param(self):
        """两个 agent 看到的描述里都不该再出现 `use_character` / `【底模】`。

        角色底模机制 2026-09-30 随 SD 渠道下线，`description_overrides` /
        `hidden_params` 也一并删了——两端现在看到的是同一份 `tool["description"]`，
        所以这条锁「两端一致且干净」。
        """
        for agent_id in (QQ_AGENT_ID, "main"):
            block = self._block(agent_id)
            self.assertNotIn("use_character", block)
            self.assertNotIn("【底模】", block)

    def test_both_see_lora_doc(self):
        self.assertIn("lora", self._block(QQ_AGENT_ID))
        self.assertIn("lora", self._block("main"))

    def test_default_image_skill_is_silver(self):
        """不点名时的默认渠道恒为 silver——2026-10-06 用户拍板改的。

        （历史：先是 `anima`，再是 `anima_realskin`、`anima_soft`，2026-09-30
        21:3x 改成 `anima_clear`，**2026-10-06 换成 silver**。原话：「我们默认
        渠道就是 sILVR，把它做成默认渠道就行了，不用加那个什么默认渠道管理」
        ——所以只有一个写死的常量，没有管理页下拉。）

        signature 的默认值必须是 None——execute_tool 是 fn(**args)，只有「模型
        压根没传 skill」才会落到默认值，靠它才分得开「没点名」和「点名了默认渠道」。
        """
        import inspect
        from app.tools.normal.generate_image import (
            _generate_image, T2I_DEFAULT_SKILL)
        self.assertIsNone(inspect.signature(_generate_image)
                          .parameters["skill"].default)
        self.assertEqual(T2I_DEFAULT_SKILL, "silver")

    def test_i2i_is_on_for_the_right_tiers_only(self):
        """图生图 2026-10-01 重开：常规档 + 高清快档 / 二档，**三档不给**。

        三档不给跟图生图的开销无关（实测只比文生图多 3~6 秒），是三档自己贵
        （1.5× + 二段 10 步，纯执行 ~150 秒）。

        2026-10-02 又加了 `qwen_image_v1`——那是**编辑**不是重绘（另一套骨架，
        见 tests/test_image_i2i.py 的 QwenEditFlowTest），动漫那 12 个一格没动。
        """
        from app.tools.normal.generate_image import (
            _I2I_ANIMA_SKILLS, _I2I_SKILLS)
        self.assertEqual(len(_I2I_ANIMA_SKILLS), 12)
        self.assertEqual(len(_I2I_SKILLS), 13)
        for s in ("anima_clear", "anima_curvy", "hd_fast_clear", "hd_2_curvy",
                  "qwen_image_v1"):
            self.assertIn(s, _I2I_SKILLS)
        for s in ("hd_3_clear", "hd_3_curvy", "krea2", "image_gen_v1", "nffa"):
            self.assertNotIn(s, _I2I_SKILLS)

    def test_description_keeps_the_look_dont_edit_boundary(self):
        """重开的边界：描述必须把「默认只看」写在前面，别教模型见引用图就垫图。

        2026-09-27 停用整条链路，就是因为模型「一看见引用图就往改图上想」。
        所以这里钉的不是「有没有这个能力」，是**什么时候才允许用**。
        """
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertIn("不传 skill 就是 silver", desc)
        self.assertIn("引用图片：默认只看，不改", desc)
        self.assertIn("看得见", desc)                  # 「看图 → 反推提示词」这条路
        self.assertIn("光是引用了图，永远不构成图生图", desc)
        self.assertIn("source_image 的门槛", desc)     # 门槛单独成段
        self.assertIn("只有明说「qwen 图生图」才传", desc)
        self.assertIn("hd_3_", desc)                   # 不支持的那档要说出来
        self.assertNotIn("图生图已停用", desc)
        # 不能有「不传 skill、只传 source_image 就自动切渠道」这种指路话
        self.assertNotIn("只传 source_image", desc)

    def test_source_image_param_states_when_to_use_it(self):
        """参数描述口径要和上面那段一致：垫的是哪张、什么时候才传、强度归谁定。

        「什么时候传 / 传了配哪个渠道」那几条钉在 `test_image_i2i_gate.py`，
        这里只补它没覆盖的三件事。
        """
        from app.tools.normal.generate_image import tool
        desc = tool["parameters"]["properties"]["source_image"]["description"]
        self.assertIn("他自己刚发的", desc)         # 没引用时垫本轮他自己发的图
        self.assertIn("默认不传", desc)
        self.assertIn("重绘强度是定死的", desc)      # denoise 不由模型调
        self.assertNotIn("已停用", desc)

    def test_description_teaches_the_one_i2i_path(self):
        """图生图现在**只有一条路**：`skill=qwen_image_v1` + `source_image`。

        模型只看得到这段文字（工作流差别它看不见），所以「哪条路能垫图」必须由
        描述说清。旧版这里钉的是「两种机制、默认走动漫重绘」——2026-10-06 用户
        拍板撤掉那条：「Anima 的图生图没有 Qwen 好用……图生图只需要一个 Qwen 的
        途径即可」。**撤的是用法文案，不是后端骨架**：`_I2I_SKILLS` 那 12 档一个
        没删（钉在上面的 `test_i2i_is_on_for_the_right_tiers_only`）。

        qwen 一张 1~2 分钟，所以「点名才走、不是改图的默认做法」的价格标签必须在。
        """
        from app.tools.normal.generate_image import tool
        desc = tool["description"]
        self.assertIn("图生图只有一条路：qwen_image_v1", desc)
        for key in ("一句改动指令", "不要把整张图重新描述",
                    "重绘已经从用法里撤掉"):
            self.assertIn(key, desc, "描述里缺了「%s」" % key)
        # qwen 那道必须带「慢」的价格标签，不能写得像默认选项
        self.assertIn("1~2 分钟", desc)
        self.assertIn("不是改图的默认做法", desc)
        # 机器人自己画的图（带渠道 + seed 那行）被引用 → 沿用原渠道重新生成
        self.assertIn("编号 / 渠道 / 种子", desc)
        # 两种模式的 prompt 写法不同，这条也得写在参数上（模型最常看的地方）
        prop = tool["parameters"]["properties"]["source_image"]["description"]
        self.assertIn("qwen_image_v1", prop)
        # 动漫 12 档一个字都没删（撤的是用法，不是能力）
        self.assertIn("anima_*", prop)
        # skill 参数不能再写「改图就选 qwen / 本机最强」这种无门槛诱导语。
        # 2026-10-04 起它改成只把渠道清单指到 Available Skills（「分不清就照
        # 那一行摘要选」），诱导语自然消失。
        skill = tool["parameters"]["properties"]["skill"]["description"]
        self.assertNotIn("最强", skill)
        self.assertNotIn("改图就选", skill)
        self.assertIn("Available Skills", skill)


if __name__ == "__main__":
    unittest.main()
